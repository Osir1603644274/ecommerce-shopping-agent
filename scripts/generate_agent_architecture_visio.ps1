[CmdletBinding()]
param(
    [string]$OutputDirectory = (Join-Path $PSScriptRoot '..\docs\diagrams')
)

$ErrorActionPreference = 'Stop'

$outputRoot = [System.IO.Path]::GetFullPath($OutputDirectory)
$previewRoot = Join-Path $outputRoot 'beijing-agent-architecture-preview'
$vsdxPath = Join-Path $outputRoot 'beijing-agent-architecture.vsdx'
$pdfPath = Join-Path $outputRoot 'beijing-agent-architecture.pdf'
$stencilPath = 'C:\Program Files\Microsoft Office\root\Office16\Visio Content\2052\BASFLO_M.VSSX'

New-Item -ItemType Directory -Force -Path $outputRoot | Out-Null
New-Item -ItemType Directory -Force -Path $previewRoot | Out-Null

$resolvedPreview = [System.IO.Path]::GetFullPath($previewRoot)
if (-not $resolvedPreview.StartsWith($outputRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "预览目录不在输出目录内：$resolvedPreview"
}
Get-ChildItem -LiteralPath $resolvedPreview -Filter '*.png' -File -ErrorAction SilentlyContinue |
    Remove-Item -Force

$C = @{
    Navy       = 'RGB(31,55,86)'
    Blue       = 'RGB(238,245,255)'
    BlueLine   = 'RGB(71,112,173)'
    Teal       = 'RGB(235,249,247)'
    TealLine   = 'RGB(47,139,126)'
    Purple     = 'RGB(246,241,255)'
    PurpleLine = 'RGB(122,87,168)'
    Green      = 'RGB(239,250,242)'
    GreenLine  = 'RGB(45,139,87)'
    Yellow     = 'RGB(255,249,230)'
    YellowLine = 'RGB(184,132,38)'
    Red        = 'RGB(255,242,241)'
    RedLine    = 'RGB(188,76,76)'
    Gray       = 'RGB(247,248,250)'
    GrayLine   = 'RGB(91,105,125)'
    White      = 'RGB(255,255,255)'
    Text       = 'RGB(30,42,59)'
    Muted      = 'RGB(91,105,125)'
    Shadow     = 'RGB(215,222,232)'
}

function Set-CellFormula {
    param(
        [Parameter(Mandatory)]$Shape,
        [Parameter(Mandatory)][string]$Cell,
        [Parameter(Mandatory)][string]$Formula
    )
    if ($Shape.CellExistsU($Cell, 0) -ne 0) {
        [void]($Shape.CellsU($Cell).FormulaU = $Formula)
    }
}

function Set-PageLayout {
    param([Parameter(Mandatory)]$Page)
    $Page.PageSheet.CellsU('PageWidth').ResultIU = 17.0
    $Page.PageSheet.CellsU('PageHeight').ResultIU = 11.0
    Set-CellFormula $Page.PageSheet 'PageLeftMargin' '0 in'
    Set-CellFormula $Page.PageSheet 'PageRightMargin' '0 in'
    Set-CellFormula $Page.PageSheet 'PageTopMargin' '0 in'
    Set-CellFormula $Page.PageSheet 'PageBottomMargin' '0 in'
}

function Add-PlainText {
    param(
        [Parameter(Mandatory)]$Page,
        [Parameter(Mandatory)][double]$X,
        [Parameter(Mandatory)][double]$Y,
        [Parameter(Mandatory)][double]$W,
        [Parameter(Mandatory)][double]$H,
        [Parameter(Mandatory)][string]$Text,
        [double]$FontSize = 7,
        [bool]$Bold = $false,
        [string]$Color = 'RGB(31,41,55)',
        [ValidateSet('left','center')][string]$Align = 'center',
        [string]$Fill = 'RGB(255,255,255)',
        [bool]$Transparent = $true
    )
    $shape = $Page.DrawRectangle($X - $W / 2, $Y - $H / 2, $X + $W / 2, $Y + $H / 2)
    $shape.Text = $Text
    Set-CellFormula $shape 'FillPattern' $(if ($Transparent) { '0' } else { '1' })
    if (-not $Transparent) {
        Set-CellFormula $shape 'FillForegnd' $Fill
        Set-CellFormula $shape 'LinePattern' '0'
    } else {
        Set-CellFormula $shape 'LinePattern' '0'
    }
    Set-CellFormula $shape 'Char.Font' 'FONT("Microsoft YaHei")'
    Set-CellFormula $shape 'Char.Size' "$FontSize pt"
    Set-CellFormula $shape 'Char.Color' $Color
    Set-CellFormula $shape 'Char.Style' $(if ($Bold) { '1' } else { '0' })
    Set-CellFormula $shape 'Para.HorzAlign' $(if ($Align -eq 'center') { '1' } else { '0' })
    Set-CellFormula $shape 'VerticalAlign' '1'
    Set-CellFormula $shape 'TextBlock.LeftMargin' '0.05 in'
    Set-CellFormula $shape 'TextBlock.RightMargin' '0.05 in'
    Set-CellFormula $shape 'TextBlock.TopMargin' '0.02 in'
    Set-CellFormula $shape 'TextBlock.BottomMargin' '0.02 in'
    return $shape
}

function Add-TitleBand {
    param(
        [Parameter(Mandatory)]$Page,
        [Parameter(Mandatory)][string]$Title,
        [Parameter(Mandatory)][string]$Subtitle,
        [Parameter(Mandatory)][int]$PageNumber
    )
    $accent = $Page.DrawRectangle(0.48, 10.34, 0.61, 10.88)
    Set-CellFormula $accent 'FillForegnd' $C.BlueLine
    Set-CellFormula $accent 'FillPattern' '1'
    Set-CellFormula $accent 'LinePattern' '0'
    Add-PlainText $Page 8.78 10.62 15.55 0.48 $Title 16 $true $C.Navy 'left' | Out-Null
    Add-PlainText $Page 8.78 10.20 15.55 0.30 $Subtitle 7.6 $false $C.Muted 'left' | Out-Null
    $rule = $Page.DrawLine(0.45, 9.97, 16.55, 9.97)
    Set-CellFormula $rule 'LineColor' $C.Navy
    Set-CellFormula $rule 'LineWeight' '1.15 pt'
    Add-PlainText $Page 1.65 0.18 2.4 0.22 '北京出行 Agent · 代码核对版' 6.1 $false $C.Muted 'left' | Out-Null
    Add-PlainText $Page 16.15 0.18 0.65 0.22 ("{0}/7" -f $PageNumber) 6.5 $true $C.Navy | Out-Null
}

function Add-FlowNode {
    param(
        [Parameter(Mandatory)]$Page,
        [Parameter(Mandatory)]$Masters,
        [Parameter(Mandatory)][hashtable]$Spec
    )
    $masterName = switch ($Spec.Type) {
        'Decision' { 'Decision' }
        'StartEnd' { 'Start/End' }
        'Data' { 'Data' }
        default { 'Process' }
    }
    $shape = $Page.Drop($Masters[$masterName], [double]$Spec.X, [double]$Spec.Y)
    $shape.CellsU('Width').ResultIU = [double]$Spec.W
    $shape.CellsU('Height').ResultIU = [double]$Spec.H
    $shape.Text = [string]$Spec.Text

    $fill = if ($Spec.Fill) { $Spec.Fill } elseif ($Spec.Type -eq 'Decision') { $C.Yellow } else { $C.Blue }
    $line = if ($Spec.Line) { $Spec.Line } elseif ($Spec.Type -eq 'Decision') { $C.YellowLine } else { $C.BlueLine }
    $requestedFontSize = if ($Spec.Font) { [double]$Spec.Font } elseif ($Spec.Type -eq 'Decision') { 6.8 } else { 7.0 }
    $fontSize = if ($Spec.Type -eq 'Decision') {
        [Math]::Max(6.6, $requestedFontSize)
    } else {
        [Math]::Max(6.5, $requestedFontSize)
    }

    Set-CellFormula $shape 'FillForegnd' $fill
    Set-CellFormula $shape 'FillPattern' '1'
    Set-CellFormula $shape 'LineColor' $line
    Set-CellFormula $shape 'LineWeight' '1.25 pt'
    Set-CellFormula $shape 'ShdwPattern' '1'
    Set-CellFormula $shape 'ShdwForegnd' $C.Shadow
    Set-CellFormula $shape 'ShdwOffsetX' '0.035 in'
    Set-CellFormula $shape 'ShdwOffsetY' '-0.035 in'
    Set-CellFormula $shape 'Char.Font' 'FONT("Microsoft YaHei")'
    Set-CellFormula $shape 'Char.Size' "$fontSize pt"
    Set-CellFormula $shape 'Char.Color' $C.Text
    Set-CellFormula $shape 'Char.Style' $(if ($Spec.Bold) { '1' } else { '0' })
    Set-CellFormula $shape 'Para.HorzAlign' '1'
    Set-CellFormula $shape 'VerticalAlign' '1'
    Set-CellFormula $shape 'TextBlock.LeftMargin' '0.11 in'
    Set-CellFormula $shape 'TextBlock.RightMargin' '0.11 in'
    Set-CellFormula $shape 'TextBlock.TopMargin' '0.06 in'
    Set-CellFormula $shape 'TextBlock.BottomMargin' '0.06 in'

    return [PSCustomObject]@{
        Id = $Spec.Id
        Shape = $shape
        X = [double]$Spec.X
        Y = [double]$Spec.Y
        W = [double]$Spec.W
        H = [double]$Spec.H
    }
}

function Get-Anchor {
    param(
        [Parameter(Mandatory)]$Node,
        [ValidateSet('top','bottom','left','right')][string]$Side
    )
    $x = [double](@($Node.X)[-1])
    $y = [double](@($Node.Y)[-1])
    $w = [double](@($Node.W)[-1])
    $h = [double](@($Node.H)[-1])
    switch ($Side) {
        'top' { return @($x, ($y + $h / 2)) }
        'bottom' { return @($x, ($y - $h / 2)) }
        'left' { return @(($x - $w / 2), $y) }
        'right' { return @(($x + $w / 2), $y) }
    }
}

function Add-FlowEdge {
    param(
        [Parameter(Mandatory)]$Page,
        [Parameter(Mandatory)]$From,
        [Parameter(Mandatory)]$To,
        [ValidateSet('top','bottom','left','right')][string]$FromSide = 'bottom',
        [ValidateSet('top','bottom','left','right')][string]$ToSide = 'top',
        [object[]]$Via = @(),
        [string]$Label = '',
        [double]$LabelX = [double]::NaN,
        [double]$LabelY = [double]::NaN,
        [string]$Color = 'RGB(75,85,99)',
        [bool]$Dashed = $false,
        [double]$LabelW = 1.15
    )
    if ([string]::IsNullOrWhiteSpace($Color)) {
        $Color = $C.GrayLine
    }
    $points = @()
    $points += ,(Get-Anchor $From $FromSide)
    foreach ($point in $Via) {
        $points += ,@([double]$point[0], [double]$point[1])
    }
    $points += ,(Get-Anchor $To $ToSide)

    for ($i = 0; $i -lt $points.Count - 1; $i++) {
        $line = $Page.DrawLine(
            [double]$points[$i][0],
            [double]$points[$i][1],
            [double]$points[$i + 1][0],
            [double]$points[$i + 1][1]
        )
        Set-CellFormula $line 'LineColor' $Color
        Set-CellFormula $line 'LineWeight' '1.35 pt'
        Set-CellFormula $line 'LinePattern' $(if ($Dashed) { '2' } else { '1' })
        if ($i -eq $points.Count - 2) {
            Set-CellFormula $line 'EndArrow' '5'
        }
        try { $line.SendToBack() } catch {}
    }

    if ($Label) {
        if ([double]::IsNaN($LabelX)) {
            $LabelX = ([double]$points[0][0] + [double]$points[1][0]) / 2
        }
        if ([double]::IsNaN($LabelY)) {
            $LabelY = ([double]$points[0][1] + [double]$points[1][1]) / 2
        }
        Add-PlainText $Page $LabelX $LabelY $LabelW 0.26 $Label 6.5 $true $Color 'center' $C.White $true | Out-Null
    }
}

function Add-PageFlow {
    param(
        [Parameter(Mandatory)]$Page,
        [Parameter(Mandatory)]$Masters,
        [Parameter(Mandatory)][hashtable[]]$NodeSpecs,
        [Parameter(Mandatory)][hashtable[]]$EdgeSpecs
    )
    $nodes = @{}
    foreach ($spec in $NodeSpecs) {
        $node = @(Add-FlowNode $Page $Masters $spec)[-1]
        $nodes[$node.Id] = $node
    }
    foreach ($edge in $EdgeSpecs) {
        $args = @{
            Page = $Page
            From = $nodes[$edge.From]
            To = $nodes[$edge.To]
        }
        foreach ($name in @('FromSide','ToSide','Via','Label','LabelX','LabelY','Color','Dashed','LabelW')) {
            if ($edge.ContainsKey($name)) {
                $args[$name] = $edge[$name]
            }
        }
        Add-FlowEdge @args
    }
}

function P {
    param([string]$Id,[double]$X,[double]$Y,[double]$W,[double]$H,[string]$Text,[string]$Fill=$C.Blue,[string]$Line=$C.BlueLine,[double]$Font=6.8)
    return @{Id=$Id;Type='Process';X=$X;Y=$Y;W=$W;H=$H;Text=$Text;Fill=$Fill;Line=$Line;Font=$Font}
}
function D {
    param([string]$Id,[double]$X,[double]$Y,[double]$W,[double]$H,[string]$Text,[double]$Font=6.6)
    return @{Id=$Id;Type='Decision';X=$X;Y=$Y;W=$W;H=$H;Text=$Text;Fill=$C.Yellow;Line=$C.YellowLine;Font=$Font;Bold=$true}
}
function T {
    param([string]$Id,[double]$X,[double]$Y,[double]$W,[double]$H,[string]$Text,[string]$Fill=$C.Green,[string]$Line=$C.GreenLine,[double]$Font=6.8)
    return @{Id=$Id;Type='StartEnd';X=$X;Y=$Y;W=$W;H=$H;Text=$Text;Fill=$Fill;Line=$Line;Font=$Font;Bold=$true}
}
function E {
    param([string]$From,[string]$To,[string]$Label='', [string]$FromSide='bottom',[string]$ToSide='top',[object[]]$Via=@(),[double]$LabelX=[double]::NaN,[double]$LabelY=[double]::NaN,[string]$Color=$C.GrayLine,[bool]$Dashed=$false,[double]$LabelW=1.15)
    $normalizedVia = @()
    $rawVia = @($Via)
    if ($rawVia.Count -gt 0 -and $rawVia[0] -isnot [System.Array]) {
        if (($rawVia.Count % 2) -ne 0) {
            throw "箭头途经点必须成对提供X/Y坐标：$From -> $To"
        }
        for ($i = 0; $i -lt $rawVia.Count; $i += 2) {
            $normalizedVia += ,@([double]$rawVia[$i], [double]$rawVia[$i + 1])
        }
    } else {
        foreach ($point in $rawVia) {
            $normalizedVia += ,@([double]$point[0], [double]$point[1])
        }
    }
    return @{From=$From;To=$To;Label=$Label;FromSide=$FromSide;ToSide=$ToSide;Via=$normalizedVia;LabelX=$LabelX;LabelY=$LabelY;Color=$Color;Dashed=$Dashed;LabelW=$LabelW}
}

$visio = $null
$document = $null
$stencil = $null

try {
    if (-not (Test-Path -LiteralPath $stencilPath)) {
        throw "找不到标准流程图模板：$stencilPath"
    }
    $visio = New-Object -ComObject Visio.InvisibleApp
    $visio.AlertResponse = 7
    $stencil = $visio.Documents.OpenEx($stencilPath, 64)
    $masters = @{
        'Process' = $stencil.Masters.ItemU('Process')
        'Decision' = $stencil.Masters.ItemU('Decision')
        'Start/End' = $stencil.Masters.ItemU('Start/End')
        'Data' = $stencil.Masters.ItemU('Data')
        'Database' = $stencil.Masters.ItemU('Database')
        'Dynamic connector' = $stencil.Masters.ItemU('Dynamic connector')
    }
    $document = $visio.Documents.Add('')

    # 1. Current production run_agent() tool loop
    $page = $document.Pages.Item(1)
    $page.Name = '01-run_agent生产循环'
    Set-PageLayout $page
    Add-TitleBand $page '1. 当前生产 run_agent() 工具调用循环' '真实生产聊天路径：仍由 llm.py::run_agent() 直接控制；此页不表示新 Harness 已接管生产。' 1
    $n = @(
        (T 's' 4.25 9.52 3.0 0.55 '开始：run_agent(message, history, TaskState?)' $C.Green $C.GreenLine 6.5),
        (D 'shortcut' 4.25 8.70 2.45 0.72 '无TaskState 且是地标商户问题？'),
        (P 'direct' 1.25 7.72 2.25 0.90 "输入：原始message`n作用：强制search_shops并渲染答案`n输出：answer + ToolTrace" $C.Teal $C.TealLine 6.0),
        (T 'directEnd' 1.25 6.67 2.25 0.50 '结束：直接返回' $C.Green $C.GreenLine 6.2),
        (P 'messages' 4.25 7.55 3.35 0.92 "输入：system/history/message/TaskState`n作用：构造messages、turn_messages、tool_traces`n输出：首轮模型上下文"),
        (D 'stateGate' 4.25 6.46 2.55 0.75 'TaskState 尚未完成内部Patch？'),
        (P 'patchState' 1.25 5.30 2.30 1.02 "输入：模型update_task_state参数`n作用：校验并OCC写TaskState`n输出：新revision + task_state上下文" $C.Yellow $C.YellowLine 5.9),
        (D 'pending' 1.25 4.12 2.25 0.72 '有pendingQuestions？'),
        (P 'ask' 1.25 2.98 2.30 0.88 "输入：最新TaskState`n作用：生成唯一必要追问`n输出：用户可见question" $C.Purple $C.PurpleLine 6.0),
        (T 'askEnd' 1.25 1.96 2.25 0.50 '结束：ask_user' $C.Green $C.GreenLine 6.2),
        (P 'menu' 4.25 5.25 3.35 1.00 "输入：routing_message`n作用：select_tool_schemas + required_tool_name`n输出：本轮schemas与allowed_tool_names"),
        (P 'model' 4.25 4.04 3.35 0.92 "输入：messages + tools + tool_choice`n作用：DeepSeek非流式规划`n输出：reply.content或tool_calls" $C.Purple $C.PurpleLine 6.2),
        (D 'calls' 4.25 2.91 2.50 0.76 'reply 有 tool_calls？'),
        (P 'final' 4.25 1.75 3.25 0.92 "输入：content或强制工具证据`n作用：约束并生成最终答案`n输出：answer + traces + turn_messages" $C.Green $C.GreenLine 6.1),
        (T 'finalEnd' 4.25 0.67 2.55 0.50 '结束：SSE/普通返回' $C.Green $C.GreenLine 6.2),
        (P 'parse' 11.15 9.22 3.45 0.92 "输入：逐个tool_call.arguments`n作用：json.loads解析`n输出：arguments或role=tool解析错误"),
        (D 'json' 11.15 8.10 2.45 0.76 '参数JSON有效？'),
        (P 'parseError' 14.75 8.10 2.35 0.88 "输入：非法arguments`n作用：回填解析错误`n输出：下一轮让模型修正" $C.Red $C.RedLine 5.9),
        (D 'allowed' 11.15 6.96 2.45 0.76 'toolName 在本轮白名单？'),
        (P 'deny' 14.75 6.96 2.35 0.88 "输入：越权toolName`n作用：不调用工具并回填错误`n输出：允许工具列表" $C.Red $C.RedLine 5.9),
        (P 'retry' 14.75 5.73 2.35 0.66 "错误已回填 → 进入下一模型轮" $C.Gray $C.GrayLine 6.1),
        (P 'override' 11.15 5.76 3.45 1.00 "输入：合法arguments + 路由语义`n作用：来源过滤/地点或商户参数护栏`n输出：最终调用参数"),
        (P 'call' 11.15 4.47 3.45 1.03 "输入：toolName + arguments`n作用：记录tool_started；call_tool()一次；记录tool_finished`n输出：ToolTrace（技术结果）" $C.Teal $C.TealLine 6.0),
        (P 'feed' 11.15 3.22 3.45 0.96 "输入：ToolTrace.detail`n作用：追加role=tool并处理歧义停止`n输出：更新后的messages/tool_traces"),
        (D 'stop' 11.15 1.98 3.05 0.82 '失败/地标/歧义，或已耗尽最大轮数？' 5.9),
        (P 'forcedFinal' 14.65 0.91 2.65 0.96 "输入：已有证据/失败事实`n作用：不带tools强制收尾`n输出：最终answer" $C.Green $C.GreenLine 5.9),
        (T 'toolEnd' 14.65 0.24 2.30 0.42 '结束：返回答案' $C.Green $C.GreenLine 5.9)
    )
    $e = @(
        (E 's' 'shortcut'),
        (E 'shortcut' 'direct' '是' 'left' 'top' @(@(2.15,8.70),@(2.15,8.30),@(1.25,8.30)) 2.35 8.70 $C.GreenLine),
        (E 'direct' 'directEnd'),
        (E 'shortcut' 'messages' '否' 'bottom' 'top' @() 4.65 8.16),
        (E 'messages' 'stateGate'),
        (E 'stateGate' 'patchState' '是' 'left' 'top' @(@(2.35,6.46),@(2.35,5.90),@(1.25,5.90)) 2.55 6.46 $C.YellowLine),
        (E 'patchState' 'pending'),
        (E 'pending' 'ask' '是' 'bottom' 'top' @() 1.63 3.55 $C.GreenLine),
        (E 'ask' 'askEnd'),
        (E 'pending' 'menu' '否' 'right' 'left' @(@(2.65,4.12),@(2.65,5.25)) 2.65 4.64),
        (E 'stateGate' 'menu' '否' 'bottom' 'top' @() 4.64 5.86),
        (E 'menu' 'model'),
        (E 'model' 'calls'),
        (E 'calls' 'final' '否' 'bottom' 'top' @() 4.63 2.32),
        (E 'final' 'finalEnd'),
        (E 'calls' 'parse' '是 → 逐个处理' 'right' 'left' @(@(6.30,2.91),@(6.30,9.22)) 6.30 6.08 $C.BlueLine $false 1.45),
        (E 'parse' 'json'),
        (E 'json' 'parseError' '否' 'right' 'left' @() 12.96 8.36 $C.RedLine),
        (E 'parseError' 'retry' '' 'right' 'right' @(@(16.20,8.10),@(16.20,5.73)) ([double]::NaN) ([double]::NaN) $C.GrayLine $true),
        (E 'json' 'allowed' '是' 'bottom' 'top' @() 11.55 7.53 $C.GreenLine),
        (E 'allowed' 'deny' '否' 'right' 'left' @() 12.96 7.22 $C.RedLine),
        (E 'deny' 'retry' '' 'bottom' 'top' @() ([double]::NaN) ([double]::NaN) $C.GrayLine $true),
        (E 'retry' 'model' '修正后重试' 'bottom' 'right' @(@(15.95,5.20),@(15.95,3.72),@(6.05,3.72)) 15.95 4.44 $C.GrayLine $true 1.35),
        (E 'allowed' 'override' '是' 'bottom' 'top' @() 11.55 6.37 $C.GreenLine),
        (E 'override' 'call'),
        (E 'call' 'feed'),
        (E 'feed' 'stop'),
        (E 'stop' 'forcedFinal' '是' 'right' 'top' @(@(13.45,1.98),@(14.65,1.98)) 13.70 2.24 $C.GreenLine),
        (E 'forcedFinal' 'toolEnd'),
        (E 'stop' 'model' '否：继续下一轮' 'left' 'right' @(@(8.80,1.98),@(8.80,4.04)) 8.80 2.90 $C.GrayLine $true 1.55)
    )
    Add-PageFlow $page $masters $n $e
    Add-PlainText $page 12.55 9.80 7.7 0.28 '生产事实：Planner / Executor / Validator / Replanner / Harness 已实现并通过测试，但尚未接管本页链路。' 6.8 $true $C.RedLine 'center' $C.Red $false | Out-Null

    # 2. Harness control flow
    $page = $document.Pages.Add()
    $page.Name = '02-Harness总体控制'
    Set-PageLayout $page
    Add-TitleBand $page '2. Harness 总体控制流程' 'run_harness_step()：先检查持久化恢复资格，再执行Planner门控、至多一个Executor步骤、Validator与可选Replanner。' 2
    $n = @(
        (T 's' 6.30 9.52 3.25 0.52 '开始：run_harness_step(state, message, tools)'),
        (D 'resumeGate' 6.30 8.66 3.15 0.74 'should_run_replanner(state)？'),
        (P 'planning' 6.30 7.54 3.85 0.98 "输入：当前TaskState / message / candidate tools`n作用：run_planning_step；按需运行Planner或读取现有Plan`n输出：HarnessPlanningStepResult.action" $C.Purple $C.PurpleLine 5.8),
        (D 'planningAction' 6.30 6.32 3.00 0.76 'planning action？'),
        (P 'ask' 1.35 5.25 2.45 0.86 "输入：ask_user`n作用：返回已有或新生成的问题`n输出：HarnessAction=ask_user" $C.Green $C.GreenLine 5.8),
        (T 'askEnd' 1.35 4.24 2.15 0.46 '结束：等待用户'),
        (P 'planStop' 3.95 5.25 2.35 0.86 "输入：stop_turn`n作用：拒绝继续推进`n输出：HarnessAction=stop_turn" $C.Red $C.RedLine 5.8),
        (T 'stopPlanEnd' 3.95 4.24 2.10 0.46 '结束：stop_turn' $C.Red $C.RedLine 5.9),
        (P 'executor' 7.20 5.12 3.55 0.98 "输入：activePlan + 最新revision + 白名单`n作用：run_executor_step；一次只执行一个PlanStep`n输出：ExecutorRunResult + 新TaskState" $C.Teal $C.TealLine 5.8),
        (D 'execAction' 7.20 3.88 2.85 0.76 'decide_after_execution？'),
        (P 'execStop' 2.75 2.86 2.55 0.84 "输入：step_failed / blocked`n作用：停止本轮`n输出：HarnessAction=stop_turn" $C.Red $C.RedLine 5.7),
        (T 'continueEnd' 7.20 2.70 2.70 0.50 '结束：continue_to_executor' $C.Green $C.GreenLine 5.8),
        (P 'validator' 10.50 2.86 3.25 0.94 "输入：完整executed Plan与持久化证据`n作用：run_validator_phase并OCC持久化`n输出：ValidatorResult + 新TaskState" $C.Yellow $C.YellowLine 5.7),
        (D 'validatorOutcome' 10.50 1.70 2.70 0.72 'ValidatorResult.outcome？'),
        (T 'completed' 13.40 0.72 2.20 0.46 'passed → task_completed'),
        (T 'stopV' 10.50 0.72 2.25 0.46 'validation_failed → stop_turn' $C.Red $C.RedLine 5.6),
        (P 'replanner' 13.70 8.20 3.70 1.10 "输入：ready Task + failed Plan + persisted insufficient ValidatorResult`n作用：run_replanner_phase；恢复入口或本轮Validator后立即进入`n输出：ReplannerResult + 新TaskState" $C.Purple $C.PurpleLine 5.5),
        (D 'replannerOutcome' 13.70 6.82 2.85 0.76 'ReplannerResult.outcome？'),
        (T 'replannedEnd' 15.45 5.62 2.55 0.48 'replanned → continue_to_executor'),
        (T 'replanAskEnd' 13.05 5.18 2.20 0.46 'needs_user_input → ask_user'),
        (T 'replanStopEnd' 10.65 5.62 2.20 0.46 'failed → stop_turn' $C.Red $C.RedLine 5.7)
    )
    $e = @(
        (E 's' 'resumeGate'),
        (E 'resumeGate' 'replanner' '是：进程恢复' 'right' 'left' @(@(8.45,8.66),@(8.45,8.20)) 9.10 8.66 $C.PurpleLine $false 1.35),
        (E 'resumeGate' 'planning' '否'),
        (E 'planning' 'planningAction'),
        (E 'planningAction' 'ask' 'ask_user' 'left' 'top' @(@(2.85,6.32),@(2.85,5.68),@(1.35,5.68)) 2.90 6.32 $C.GreenLine $false 1.25),
        (E 'ask' 'askEnd'),
        (E 'planningAction' 'planStop' 'stop_turn' 'left' 'top' @(@(4.80,6.32),@(4.80,5.68),@(3.95,5.68)) 4.80 6.05 $C.RedLine $false 1.20),
        (E 'planStop' 'stopPlanEnd'),
        (E 'planningAction' 'executor' 'continue_to_executor'),
        (E 'planningAction' 'validator' 'ready_for_validation' 'right' 'top' @(@(9.25,6.32),@(9.25,3.42),@(10.50,3.42)) 9.25 4.48 $C.YellowLine $false 1.55),
        (E 'executor' 'execAction'),
        (E 'execAction' 'execStop' 'stop_turn' 'left' 'top' @(@(4.45,3.88),@(4.45,3.28),@(2.75,3.28)) 4.45 3.88 $C.RedLine $false 1.15),
        (E 'execAction' 'continueEnd' 'continue_to_executor' 'bottom' 'top' @() 7.85 3.26 $C.GreenLine $false 1.55),
        (E 'execAction' 'validator' 'ready_for_validation' 'right' 'top' @(@(9.00,3.88),@(9.00,3.32),@(10.50,3.32)) 9.00 3.60 $C.YellowLine $false 1.50),
        (E 'validator' 'validatorOutcome'),
        (E 'validatorOutcome' 'completed' 'passed' 'right' 'top' @(@(13.40,1.70),@(13.40,1.18)) 12.45 1.70 $C.GreenLine),
        (E 'validatorOutcome' 'stopV' 'validation_failed'),
        (E 'validatorOutcome' 'replanner' 'insufficient：立即恢复' 'right' 'right' @(@(16.35,1.70),@(16.35,8.20)) 16.35 4.65 $C.PurpleLine $true 1.55),
        (E 'replanner' 'replannerOutcome'),
        (E 'replannerOutcome' 'replannedEnd' 'replanned' 'right' 'top' @(@(15.45,6.82)) 15.05 7.08 $C.GreenLine),
        (E 'replannerOutcome' 'replanAskEnd' 'needs_user_input' 'bottom' 'top' @(@(13.70,5.63),@(13.05,5.63)) 13.70 5.92 $C.GreenLine $false 1.50),
        (E 'replannerOutcome' 'replanStopEnd' 'replanning_failed' 'left' 'top' @(@(11.75,6.82),@(11.75,5.62),@(10.65,5.62)) 11.75 6.18 $C.RedLine $false 1.50)
    )
    Add-PageFlow $page $masters $n $e
    Add-PlainText $page 13.15 9.55 6.2 0.34 '生产边界：显式Harness仍未接管 llm.py::run_agent()；新Plan只在后续Harness step交给Executor。' 6.2 $true $C.RedLine 'center' $C.Red $false | Out-Null

    # 3. Planner details
    $page = $document.Pages.Add()
    $page.Name = '03-Planner详细流程'
    Set-PageLayout $page
    Add-TitleBand $page '3. Planner 详细流程：parse、accept、修复重试与三种结果' '模型只提交 PlannerModelOutput；Runtime拥有planId、basedOnRevision、状态、校验与持久化职责。' 3
    $n = @(
        (T 's' 4.05 9.48 2.65 0.50 '开始：run_planner_phase'),
        (P 'context' 4.05 8.62 3.45 0.90 "输入：TaskState/message/schemas/policies`n作用：构造revision-bound PlannerContext`n输出：candidateTools + expectedOutputContracts"),
        (D 'prePending' 4.05 7.58 2.50 0.72 'context 有pendingQuestions？'),
        (D 'preReady' 4.05 6.56 2.40 0.72 'taskStatus == ready？'),
        (P 'modelCall' 4.05 5.46 3.45 0.92 "输入：messages + submit_planner_output schema`n作用：强制模型提交结构化结果`n输出：reply.tool_calls（attempt 1/2）" $C.Purple $C.PurpleLine 5.9),
        (D 'parseOK' 4.05 4.33 2.70 0.78 '恰好一次调用 + JSON对象 + Pydantic通过？' 5.8),
        (D 'attempt' 4.05 3.22 2.30 0.72 '这是第1次失败？'),
        (P 'repair' 1.25 2.14 2.30 0.92 "输入：errorCode + reason`n作用：追加修复提示并再调用一次`n输出：attempt 2 reply" $C.Purple $C.PurpleLine 5.8),
        (P 'failure' 4.05 1.96 2.65 0.88 "输入：最终校验失败`n作用：构造PlannerResult`n输出：planning_failed" $C.Red $C.RedLine 5.9),
        (P 'persistFail' 4.05 0.84 2.75 0.84 "输入：planning_failed + current revision`n作用：OCC写planningFailure`n输出：TaskState revision+1" $C.Red $C.RedLine 5.8),
        (D 'modelOutcome' 10.20 9.25 2.60 0.74 'PlannerModelOutput.outcome？'),
        (P 'needs' 14.55 8.74 2.55 0.84 "输入：模型question或已有pendingQuestion`n作用：构造needs_user_input`n输出：PlannerResult.question" $C.Green $C.GreenLine 5.8),
        (D 'ctxPending' 10.20 8.10 2.55 0.72 'Context仍有pendingQuestions？'),
        (D 'ctxReady' 10.20 7.08 2.35 0.72 'TaskState仍ready？'),
        (P 'validate' 10.20 5.92 3.65 1.06 "输入：逐个PlanStepProposal`n作用：校验工具候选、必要/多余参数、声明来源真实值、prior_step顺序、expectedOutput合同`n输出：可接受proposal或PlannerValidationError" $C.Yellow $C.YellowLine 5.7),
        (D 'valid' 10.20 4.68 2.55 0.74 '所有Proposal均通过？'),
        (P 'runtimePlan' 10.20 3.55 3.55 0.96 "输入：已验证proposals + taskRevision`n作用：Runtime生成planId/basedOnRevision；Proposal转pending PlanStep`n输出：active TaskPlan" $C.Blue $C.BlueLine 5.8),
        (P 'planned' 10.20 2.38 2.80 0.82 "输入：TaskPlan`n作用：构造PlannerResult`n输出：outcome=planned" $C.Green $C.GreenLine 5.9),
        (P 'persistPlanned' 10.20 1.22 3.10 0.90 "输入：planned + current revision`n作用：expectedRevision OCC写activePlan`n输出：TaskState revision+1" $C.Green $C.GreenLine 5.8),
        (P 'persistNeeds' 14.55 7.44 2.70 0.88 "输入：needs_user_input`n作用：OCC写collecting_information + pendingQuestions`n输出：TaskState revision+1" $C.Green $C.GreenLine 5.7),
        (T 'plannedEnd' 10.20 0.28 2.55 0.42 '结束：planned'),
        (T 'needsEnd' 14.55 6.32 2.40 0.42 '结束：needs_user_input'),
        (T 'failEnd' 4.05 0.22 2.20 0.40 '结束：planning_failed' $C.Red $C.RedLine 5.8)
    )
    $e = @(
        (E 's' 'context'),
        (E 'context' 'prePending'),
        (E 'prePending' 'needs' '是' 'right' 'left' @(@(7.00,7.58),@(7.00,8.74)) 7.00 8.18 $C.GreenLine),
        (E 'prePending' 'preReady' '否'),
        (E 'preReady' 'failure' '否：task_not_ready' 'left' 'top' @(@(2.45,6.56),@(2.45,2.55),@(4.05,2.55)) 2.45 4.40 $C.RedLine $false 1.55),
        (E 'preReady' 'modelCall' '是'),
        (E 'modelCall' 'parseOK'),
        (E 'parseOK' 'modelOutcome' '是' 'right' 'left' @(@(6.35,4.33),@(6.35,9.25)) 6.35 6.85 $C.GreenLine),
        (E 'parseOK' 'attempt' '否'),
        (E 'attempt' 'repair' '是' 'left' 'top' @(@(2.35,3.22),@(2.35,2.60),@(1.25,2.60)) 2.55 3.22 $C.PurpleLine),
        (E 'repair' 'modelCall' '修复重试' 'top' 'left' @(@(1.25,5.46)) 1.25 4.00 $C.PurpleLine),
        (E 'attempt' 'failure' '否'),
        (E 'failure' 'persistFail'),
        (E 'persistFail' 'failEnd'),
        (E 'modelOutcome' 'needs' 'needs_user_input' 'right' 'top' @(@(12.40,9.25),@(14.55,9.25)) 13.35 9.50 $C.GreenLine $false 1.55),
        (E 'modelOutcome' 'ctxPending' 'planned'),
        (E 'ctxPending' 'needs' '有' 'right' 'left' @() 12.35 8.36 $C.GreenLine),
        (E 'ctxPending' 'ctxReady' '没有'),
        (E 'ctxReady' 'failure' '不是' 'left' 'right' @(@(7.65,7.08),@(7.65,1.96)) 7.65 4.55 $C.RedLine),
        (E 'ctxReady' 'validate' '是'),
        (E 'validate' 'valid'),
        (E 'valid' 'failure' '否' 'left' 'right' @(@(7.55,4.68),@(7.55,1.96)) 7.55 3.28 $C.RedLine),
        (E 'valid' 'runtimePlan' '是'),
        (E 'runtimePlan' 'planned'),
        (E 'planned' 'persistPlanned'),
        (E 'persistPlanned' 'plannedEnd'),
        (E 'needs' 'persistNeeds'),
        (E 'persistNeeds' 'needsEnd')
    )
    Add-PageFlow $page $masters $n $e
    Add-PlainText $page 13.55 5.18 5.4 0.34 'accepted Plan 的执行合同不可原地改写；后续只允许状态推进。' 6.6 $true $C.RedLine 'center' $C.Red $false | Out-Null

    # 4. Executor single-step flow
    $page = $document.Pages.Add()
    $page.Name = '04-Executor单步流程'
    Set-PageLayout $page
    Add-TitleBand $page '4. Executor 单步流程' '选择 → OCC领取 → 参数来源解析 → 白名单/Schema → 单次调用 → 结果提取 → OCC落盘；每次最多一个PlanStep。' 4
    $n = @(
        (T 's' 4.10 9.48 2.70 0.50 '开始：run_executor_step'),
        (P 'select' 4.10 8.57 3.40 0.92 "输入：TaskState.activePlan`n作用：选择首个pending且前序均executed步骤`n输出：revision-bound ExecutorStepContext"),
        (D 'selectOK' 4.10 7.49 2.50 0.74 '任务/Plan/步骤可执行？'),
        (P 'selectStop' 1.25 6.38 2.30 0.86 "输入：选择异常`n作用：拒绝跳步或重复领取`n输出：ExecutorSelectionError" $C.Red $C.RedLine 5.8),
        (P 'claim' 4.10 6.30 3.40 0.96 "输入：expectedRevision + pending step`n作用：OCC Patch将Task ready→executing、Step pending→executing`n输出：claimed TaskState" $C.Yellow $C.YellowLine 5.8),
        (D 'claimOK' 4.10 5.16 2.40 0.72 'OCC领取成功？'),
        (P 'claimStop' 1.25 4.05 2.30 0.86 "输入：revision/身份冲突`n作用：拒绝旧快照领取`n输出：冲突异常" $C.Red $C.RedLine 5.8),
        (P 'resolve' 4.10 3.95 3.45 1.04 "输入：accepted PlanStep.argumentSources`n作用：解析task_goal/task_state/system_policy/prior_step；对非prior值检查未变化`n输出：ResolvedExecutorArguments"),
        (D 'resolveOK' 4.10 2.72 2.45 0.74 '参数来源解析成功？'),
        (P 'blockArg' 1.25 1.60 2.35 0.92 "输入：解析错误`n作用：OCC写blocked + executorBlock`n输出：step_blocked / Task ready" $C.Red $C.RedLine 5.7),
        (P 'validate' 10.35 9.15 3.45 1.00 "输入：resolvedArguments + allowed schemas`n作用：toolName白名单 + Draft2020-12 JSON Schema严格校验`n输出：ValidatedExecutorToolCall" $C.Yellow $C.YellowLine 5.8),
        (D 'validateOK' 10.35 7.96 2.50 0.74 '白名单与Schema通过？'),
        (P 'blockValidation' 14.65 7.96 2.60 0.90 "输入：校验错误`n作用：OCC写blocked + executorBlock`n输出：step_blocked" $C.Red $C.RedLine 5.7),
        (P 'dispatch' 10.35 6.77 3.50 1.04 "输入：ValidatedExecutorToolCall`n作用：execute_validated_tool_call；只调用一次tool_caller`n输出：不可变StepExecutionResult" $C.Teal $C.TealLine 5.8),
        (D 'technical' 10.35 5.50 2.65 0.78 'outcome == tool_succeeded？'),
        (P 'persistFailed' 14.65 5.50 2.65 0.94 "输入：tool_failed/tool_error`n作用：OCC记录执行历史并Step→failed`n输出：step_failed / Task ready" $C.Red $C.RedLine 5.7),
        (D 'referenced' 10.35 4.27 2.70 0.76 '后续步骤引用本步输出？'),
        (P 'extract' 10.35 3.10 3.45 0.98 "输入：成功ToolTrace + expectedOutput`n作用：提取稳定NormalizedStepOutput（如唯一shopId）`n输出：stepOutputs记录"),
        (D 'extractOK' 10.35 1.93 2.55 0.74 '输出提取成功？'),
        (P 'persistBlocked' 14.65 1.93 2.65 0.92 "输入：提取错误`n作用：OCC记录技术结果并Step→blocked`n输出：step_blocked / Task ready" $C.Red $C.RedLine 5.7),
        (P 'persistExecuted' 7.20 0.92 3.35 0.92 "输入：tool_succeeded + 可选NormalizedOutput`n作用：OCC记录历史/输出并Step→executed`n输出：Task ready, revision+1" $C.Green $C.GreenLine 5.7),
        (T 'end' 10.35 0.42 2.85 0.46 '结束：step_executed（仍待Validator）')
    )
    $e = @(
        (E 's' 'select'),
        (E 'select' 'selectOK'),
        (E 'selectOK' 'selectStop' '否' 'left' 'top' @(@(2.45,7.49),@(2.45,6.81),@(1.25,6.81)) 2.55 7.49 $C.RedLine),
        (E 'selectOK' 'claim' '是'),
        (E 'claim' 'claimOK'),
        (E 'claimOK' 'claimStop' '否' 'left' 'top' @(@(2.45,5.16),@(2.45,4.48),@(1.25,4.48)) 2.55 5.16 $C.RedLine),
        (E 'claimOK' 'resolve' '是'),
        (E 'resolve' 'resolveOK'),
        (E 'resolveOK' 'blockArg' '否' 'left' 'top' @(@(2.45,2.72),@(2.45,2.06),@(1.25,2.06)) 2.55 2.72 $C.RedLine),
        (E 'resolveOK' 'validate' '是 → 预派单校验' 'right' 'left' @(@(6.25,2.72),@(6.25,9.15)) 6.25 5.88 $C.BlueLine $false 1.55),
        (E 'validate' 'validateOK'),
        (E 'validateOK' 'blockValidation' '否' 'right' 'left' @() 12.55 8.22 $C.RedLine),
        (E 'validateOK' 'dispatch' '是'),
        (E 'dispatch' 'technical'),
        (E 'technical' 'persistFailed' '否' 'right' 'left' @() 12.55 5.76 $C.RedLine),
        (E 'technical' 'referenced' '是：仅技术成功' 'bottom' 'top' @() 10.95 4.88 $C.GreenLine $false 1.55),
        (E 'referenced' 'persistExecuted' '否' 'left' 'top' @(@(7.20,4.27),@(7.20,1.40)) 7.20 3.00 $C.GreenLine),
        (E 'referenced' 'extract' '是'),
        (E 'extract' 'extractOK'),
        (E 'extractOK' 'persistBlocked' '否' 'right' 'left' @() 12.55 2.19 $C.RedLine),
        (E 'extractOK' 'persistExecuted' '是' 'left' 'top' @(@(8.20,1.93),@(8.20,1.40),@(7.20,1.40)) 8.20 1.62 $C.GreenLine),
        (E 'persistExecuted' 'end' '' 'right' 'left')
    )
    Add-PageFlow $page $masters $n $e
    Add-PlainText $page 13.15 3.66 6.0 0.36 '关键语义：tool_succeeded 只说明工具技术调用成功，不等于 Validator passed。' 6.8 $true $C.RedLine 'center' $C.Red $false | Out-Null

    # 5. Validator flow
    $page = $document.Pages.Add()
    $page.Name = '05-Validator流程'
    Set-PageLayout $page
    Add-TitleBand $page '5. Validator 流程' '进入条件 → 证据组装 → 逐步确定性验证 → 聚合 → revision绑定OCC持久化 → HarnessAction。' 5
    $n = @(
        (T 's' 4.15 9.48 2.70 0.50 '开始：run_validator_phase'),
        (D 'entry' 4.15 8.55 3.20 0.82 'Task ready、无问题、Plan active、全部Step executed？' 5.7),
        (P 'entryStop' 1.25 7.38 2.35 0.88 "输入：不满足进入条件`n作用：拒绝开始Validator`n输出：ValidatorSelectionError" $C.Red $C.RedLine 5.8),
        (P 'context' 4.15 7.27 3.50 1.02 "输入：activePlan + domainState执行历史/stepOutputs`n作用：按taskId/planId/stepId组装最新证据`n输出：revision-bound ValidatorContext"),
        (D 'evidenceOK' 4.15 6.07 2.50 0.74 '证据结构与身份有效？'),
        (P 'evidenceFail' 1.25 4.95 2.50 0.90 "输入：损坏/错配证据`n作用：构造validation_failed`n输出：ValidatorResult" $C.Red $C.RedLine 5.7),
        (P 'stepValidate' 4.15 4.78 3.55 1.12 "输入：每个executed PlanStep`n作用：检查成功执行身份、expectedOutput声明，并运行requiresShopId / ShopDetail / ReviewEvidence合同`n输出：StepValidationResult[]" $C.Yellow $C.YellowLine 5.6),
        (D 'stepOutcome' 4.15 3.44 2.75 0.78 '步骤结果属于哪一层？'),
        (P 'invalid' 1.25 2.25 2.45 0.88 "输入：invalid_evidence`n作用：标记不可接受/合同非法`n输出：validation_failed" $C.Red $C.RedLine 5.7),
        (P 'insufficient' 4.15 2.22 2.55 0.92 "输入：insufficient_evidence`n作用：说明工具成功但证据不足`n输出：insufficient_evidence" $C.Red $C.RedLine 5.6),
        (P 'satisfied' 7.15 2.25 2.45 0.88 "输入：全部合同满足`n作用：聚合全部步骤`n输出：passed" $C.Green $C.GreenLine 5.7),
        (P 'result' 10.55 8.90 3.45 1.00 "输入：聚合outcome + taskRevision`n作用：生成ValidatorResult.basedOnRevision`n输出：passed / insufficient / failed" $C.Blue $C.BlueLine 5.8),
        (D 'match' 10.55 7.67 2.75 0.78 'result身份与当前revision匹配？'),
        (P 'occReject' 14.75 7.67 2.55 0.90 "输入：旧ValidatorResult`n作用：拒绝过时结论`n输出：validation_result_mismatch/OCC冲突" $C.Red $C.RedLine 5.6),
        (P 'persist' 10.55 6.43 3.65 1.06 "输入：ValidatorResult`n作用：Patch.expectedRevision=current revision；同一Patch更新Plan/Task并保存validationResult`n输出：TaskState revision+1" $C.Yellow $C.YellowLine 5.7),
        (D 'outcome' 10.55 5.12 2.65 0.78 'ValidatorResult.outcome？'),
        (P 'passedPersist' 14.75 4.45 2.60 0.90 "输入：passed`n作用：Plan→completed；Task→completed`n输出：HarnessAction=task_completed" $C.Green $C.GreenLine 5.6),
        (P 'insPersist' 10.55 3.76 2.95 0.94 "输入：insufficient_evidence`n作用：Plan→failed；Task→ready；Harness立即调用Replanner`n输出：进入run_replanner_phase" $C.Purple $C.PurpleLine 5.5),
        (P 'failPersist' 7.25 4.45 2.65 0.90 "输入：validation_failed`n作用：Plan→failed；Task→ready`n输出：HarnessAction=stop_turn" $C.Red $C.RedLine 5.6),
        (T 'passedEnd' 14.75 3.25 2.20 0.46 '结束：task_completed'),
        (T 'replanEnd' 10.55 2.54 2.75 0.46 '转入：run_replanner_phase' $C.Purple $C.PurpleLine 5.7),
        (T 'failEnd' 7.25 3.25 2.15 0.46 '结束：stop_turn' $C.Red $C.RedLine 5.8)
    )
    $e = @(
        (E 's' 'entry'),
        (E 'entry' 'entryStop' '否' 'left' 'top' @(@(2.40,8.55),@(2.40,7.82),@(1.25,7.82)) 2.55 8.55 $C.RedLine),
        (E 'entry' 'context' '是'),
        (E 'context' 'evidenceOK'),
        (E 'evidenceOK' 'evidenceFail' '否' 'left' 'top' @(@(2.40,6.07),@(2.40,5.40),@(1.25,5.40)) 2.55 6.07 $C.RedLine),
        (E 'evidenceOK' 'stepValidate' '是'),
        (E 'stepValidate' 'stepOutcome'),
        (E 'stepOutcome' 'invalid' 'invalid' 'left' 'top' @(@(2.50,3.44),@(2.50,2.70),@(1.25,2.70)) 2.55 3.44 $C.RedLine),
        (E 'stepOutcome' 'insufficient' 'insufficient'),
        (E 'stepOutcome' 'satisfied' 'satisfied' 'right' 'top' @(@(5.90,3.44),@(5.90,2.70),@(7.15,2.70)) 5.90 3.70 $C.GreenLine),
        (E 'evidenceFail' 'result' 'validation_failed' 'right' 'left' @(@(2.85,4.95),@(2.85,9.82),@(8.85,9.82),@(8.85,8.90)) 5.75 9.82 $C.GrayLine $true 1.55),
        (E 'invalid' 'result' 'validation_failed' 'bottom' 'left' @(@(1.25,0.95),@(8.85,0.95),@(8.85,8.90)) 4.85 0.95 $C.GrayLine $true 1.55),
        (E 'insufficient' 'result' 'insufficient_evidence' 'right' 'left' @(@(5.80,2.22),@(5.80,8.65),@(8.35,8.65)) 5.80 5.50 $C.GrayLine $true 1.60),
        (E 'satisfied' 'result' 'passed' 'right' 'left' @(@(8.85,2.25),@(8.85,8.90)) 8.85 5.65 $C.GrayLine $true),
        (E 'result' 'match'),
        (E 'match' 'occReject' '否' 'right' 'left' @() 12.70 7.93 $C.RedLine),
        (E 'match' 'persist' '是'),
        (E 'persist' 'outcome'),
        (E 'outcome' 'passedPersist' 'passed' 'right' 'left' @(@(12.50,5.12),@(12.50,4.45)) 12.50 4.80 $C.GreenLine),
        (E 'outcome' 'insPersist' 'insufficient'),
        (E 'outcome' 'failPersist' 'failed' 'left' 'right' @(@(8.75,5.12),@(8.75,4.45)) 8.75 4.80 $C.RedLine),
        (E 'passedPersist' 'passedEnd'),
        (E 'insPersist' 'replanEnd'),
        (E 'failPersist' 'failEnd')
    )
    Add-PageFlow $page $masters $n $e
    Add-PlainText $page 12.55 1.48 7.2 0.36 'tool_succeeded ≠ Validator passed：技术调用成功后仍必须检查服务器定义的证据合同。' 6.8 $true $C.RedLine 'center' $C.Red $false | Out-Null
    Add-PlainText $page 12.55 0.92 7.2 0.34 'insufficient_evidence 会进入最小Replanner；validation_failed仍停止本轮，避免沿用不可信证据。' 6.4 $true $C.PurpleLine 'center' $C.Purple $false | Out-Null

    # 6. Replanner recovery flow
    $page = $document.Pages.Add()
    $page.Name = '06-Replanner恢复流程'
    Set-PageLayout $page
    Add-TitleBand $page '6. Replanner 恢复流程' '两个Harness入口汇合到同一最小恢复流程；只自动处理insufficient_evidence，不修改旧Plan。' 6
    $n = @(
        (P 'entryNow' 2.45 9.45 3.55 0.66 "入口1：本轮Validator产生insufficient_evidence`n输出：立即进入Replanner" $C.Teal $C.TealLine 5.6),
        (T 's' 8.50 9.45 3.15 0.50 '开始：run_replanner_phase'),
        (P 'entryResume' 14.20 9.45 4.00 0.66 "入口2：进程恢复读取ready Task + failed Plan + persisted insufficient ValidatorResult`n输出：跳过Planner进入Replanner" $C.Teal $C.TealLine 5.3),
        (D 'entry' 8.50 8.50 5.05 0.78 '恢复资格：ready、无pendingQuestions、failed activePlan、failure.outcome == insufficient_evidence？' 5.4),
        (P 'entryStop' 2.20 8.15 2.85 0.84 "输入：不满足恢复边界`n作用：拒绝自动重规划`n输出：ReplannerSelectionError" $C.Red $C.RedLine 5.6),
        (T 'entryStopEnd' 2.20 7.18 2.20 0.44 '结束：stop_turn' $C.Red $C.RedLine 5.7),
        (P 'context' 8.50 7.25 8.20 1.20 "Input / 输入：TaskState目标/事实/约束/revision；failedPlan；ValidatorResult(errorCode/reason)；持久化NormalizedStepOutput；candidateTools + expectedOutputContracts；replanAttempt/maxReplanAttempts；systemPolicies`nResponsibility / 作用：build_replanner_context() 从可信持久化证据构造恢复视图`nOutput / 输出：revision-bound、frozen/read-only ReplannerContext" $C.Purple $C.PurpleLine 5.1),
        (D 'attempt' 8.50 6.18 3.30 0.66 'replanAttempt ≤ maxReplanAttempts？' 5.6),
        (P 'model' 8.50 5.22 5.35 0.92 "输入：ReplannerContext + submit_replanner_output Schema`n作用：调用LLM；_parse_replanner_model_output()解析唯一tool call`n输出：ReplannerModelOutput或结构错误" $C.Purple $C.PurpleLine 5.5),
        (D 'parsed' 8.50 4.30 3.20 0.64 'parse / Pydantic结构有效？' 5.6),
        (P 'repair' 3.05 4.30 3.10 0.88 "输入：parse错误或服务器验收失败`n作用：追加errorCode/reason；最多一次结构修复重试`n输出：第2次reply或replanning_failed" $C.Purple $C.PurpleLine 5.3),
        (D 'modelOutcome' 8.50 3.46 3.00 0.64 'ReplannerModelOutput.outcome？' 5.6),
        (P 'failed' 2.30 2.74 3.15 0.96 "输入：次数耗尽、模型错误或两次均未通过`n作用：构造并持久化errorCode/reason；attempt计数+1`n输出：ReplannerResult.replanning_failed" $C.Red $C.RedLine 5.3),
        (T 'failEnd' 2.30 1.60 2.45 0.46 '结束：HarnessAction=stop_turn' $C.Red $C.RedLine 5.6),
        (P 'accept' 8.50 2.55 6.00 1.18 "输入：replanned PlanStepProposal[]`n作用：accept_replanner_model_output()复用Planner验收：工具白名单、必要/多余参数、argumentSources、expectedOutput合同、prior_step顺序；并以tool/arguments/sources/expectedOutput签名拒绝replan_unchanged`n输出：accepted proposals或验收失败" $C.Yellow $C.YellowLine 5.0),
        (D 'accepted' 8.50 1.75 3.00 0.64 '新执行合同通过？' 5.6),
        (P 'persistNew' 8.50 0.86 6.10 0.72 "输入：accepted proposals + current revision`n作用：Runtime生成新planId、Plan.basedOnRevision=current revision、全部Step=pending；persist_replanner_result()以Patch.expectedRevision做OCC写入`n输出：新activePlan + Task revision+1；旧结果冲突时拒绝覆盖" $C.Blue $C.BlueLine 5.1),
        (T 'continue' 8.50 0.34 3.45 0.34 '结束：HarnessAction=continue_to_executor'),
        (P 'ask' 14.25 2.74 3.30 0.96 "输入：needs_user_input + question`n作用：OCC写TaskState collecting_information与pendingQuestions；不增加replanAttemptCount`n输出：ReplannerResult.needs_user_input" $C.Green $C.GreenLine 5.2),
        (T 'askEnd' 14.25 1.60 2.65 0.46 '结束：HarnessAction=ask_user')
    )
    $e = @(
        (E 'entryNow' 's' '本轮恢复' 'right' 'left' @() 5.45 9.70 $C.TealLine),
        (E 'entryResume' 's' '恢复调度' 'left' 'right' @() 11.35 9.70 $C.TealLine),
        (E 's' 'entry'),
        (E 'entry' 'entryStop' '否' 'left' 'right' @() 5.30 8.55 $C.RedLine),
        (E 'entryStop' 'entryStopEnd'),
        (E 'entry' 'context' '是'),
        (E 'context' 'attempt'),
        (E 'attempt' 'failed' '否：次数耗尽' 'left' 'left' @(@(0.55,6.18),@(0.55,2.74)) 1.20 4.85 $C.RedLine $true 1.35),
        (E 'attempt' 'model' '是'),
        (E 'model' 'parsed'),
        (E 'parsed' 'repair' '否' 'left' 'right' @() 5.60 4.56 $C.RedLine),
        (E 'repair' 'model' '首次失败：修复一次' 'top' 'left' @(@(3.05,5.22),@(5.82,5.22)) 4.45 5.47 $C.PurpleLine $true 1.55),
        (E 'repair' 'failed' '第2次仍失败' 'bottom' 'top' @() 2.95 3.50 $C.RedLine),
        (E 'parsed' 'modelOutcome' '是'),
        (E 'modelOutcome' 'ask' 'needs_user_input' 'right' 'top' @(@(12.15,3.46),@(12.15,3.22),@(14.25,3.22)) 12.55 3.72 $C.GreenLine $false 1.55),
        (E 'modelOutcome' 'accept' 'replanned'),
        (E 'accept' 'accepted'),
        (E 'accepted' 'repair' '否：服务器拒绝' 'left' 'bottom' @(@(5.05,1.75),@(5.05,3.72),@(3.05,3.72)) 5.05 2.70 $C.RedLine $true 1.55),
        (E 'accepted' 'persistNew' '是' 'bottom' 'top' @() 8.90 1.32 $C.GreenLine),
        (E 'persistNew' 'continue'),
        (E 'ask' 'askEnd'),
        (E 'failed' 'failEnd')
    )
    Add-PageFlow $page $masters $n $e
    Add-PlainText $page 14.25 0.72 4.45 0.36 '边界：validation_failed仍stop_turn；新Plan不是原地改写旧Plan。' 6.2 $true $C.RedLine 'center' $C.Red $false | Out-Null

    # 7. TaskState Patch and OCC
    $page = $document.Pages.Add()
    $page.Name = '07-TaskState-Patch-OCC'
    Set-PageLayout $page
    Add-TitleBand $page '7. TaskState Patch 与 OCC 流程' 'Plan.basedOnRevision 固化计划生成依据；Patch.expectedRevision 保护每一次具体写入。两者职责不同。' 7
    $n = @(
        (T 's' 4.10 9.48 2.80 0.50 '开始：调用方准备TaskState Patch'),
        (P 'prepare' 4.10 8.54 3.60 1.00 "输入：读取到的TaskState revision=N`n作用：设置Patch.expectedRevision=N；若创建新Plan则Plan.basedOnRevision=N`n输出：待提交Patch" $C.Blue $C.BlueLine 5.8),
        (P 'lock' 4.10 7.30 3.45 0.94 "输入：taskId + Patch`n作用：获取同taskId异步锁并读取Redis当前快照`n输出：current TaskState" $C.Yellow $C.YellowLine 5.8),
        (D 'revision' 4.10 6.14 2.65 0.76 'expectedRevision == current.revision？'),
        (P 'conflict' 1.25 5.02 2.45 0.90 "输入：expected≠actual`n作用：拒绝合并旧Patch`n输出：TaskStateRevisionConflictError" $C.Red $C.RedLine 5.7),
        (P 'merge' 4.10 4.88 3.55 1.08 "输入：合法Patch + current snapshot`n作用：校验状态转换；upsert/remove事实约束；add/resolve unknown；浅合并domainState`n输出：候选updated state"),
        (D 'planChange' 4.10 3.57 2.65 0.76 'Patch包含activePlan变更？'),
        (D 'newPlan' 4.10 2.47 2.50 0.74 '是新planId / 首次Plan？'),
        (D 'newValid' 1.25 1.35 2.45 0.78 'basedOn=current rev 且 active/all pending？' 5.7),
        (D 'existingValid' 7.15 1.35 2.75 0.82 '同planId仅推进允许状态，执行合同完全不变？' 5.6),
        (P 'rejectPlan' 4.10 0.62 2.70 0.62 "输入：非法Plan修改`n作用：拒绝写入`n输出：TaskStateTransitionError" $C.Red $C.RedLine 5.5),
        (D 'completion' 10.80 9.10 3.00 0.80 'completed Task 与 completed Plan 同一Patch一致？' 5.7),
        (P 'build' 10.80 7.90 3.55 1.00 "输入：已通过全部不变量的候选状态`n作用：revision=N+1，更新时间，保留不可变合同`n输出：updated TaskState"),
        (P 'save' 10.80 6.65 3.55 1.02 "输入：updated TaskState`n作用：覆盖写Redis、刷新TTL、append updated事件`n输出：持久化快照 + 审计事件" $C.Teal $C.TealLine 5.8),
        (T 'end' 10.80 5.42 2.80 0.50 '结束：返回revision=N+1'),
        (P 'planRevision' 14.70 3.82 2.80 1.12 "输入：Planner或Replanner读取的事实快照`n作用：Plan.basedOnRevision记录合同生成依据，计划生命周期内不变`n输出：可审计Plan合同" $C.Blue $C.BlueLine 5.6),
        (P 'patchRevision' 14.70 2.42 2.80 1.12 "输入：任一组件准备写入时读取的快照`n作用：Patch.expectedRevision保护该次CAS式写入，每次随当前revision变化`n输出：成功写入或冲突" $C.Yellow $C.YellowLine 5.7),
        (P 'immutability' 10.80 3.05 3.35 1.08 "输入：同planId proposed Plan`n作用：禁止改tool/arguments/sources/expectedOutput/步骤集合/创建依据`n输出：仅允许状态推进" $C.Red $C.RedLine 5.7),
        (T 'replan' 10.80 1.72 3.55 0.52 '路线变化：Replanner创建新planId与全pending步骤' $C.Purple $C.PurpleLine 5.6)
    )
    $e = @(
        (E 's' 'prepare'),
        (E 'prepare' 'lock'),
        (E 'lock' 'revision'),
        (E 'revision' 'conflict' '否' 'left' 'top' @(@(2.40,6.14),@(2.40,5.47),@(1.25,5.47)) 2.55 6.14 $C.RedLine),
        (E 'revision' 'merge' '是'),
        (E 'merge' 'planChange'),
        (E 'planChange' 'newPlan' '是'),
        (E 'planChange' 'completion' '否：无Plan变更' 'right' 'left' @(@(6.20,3.57),@(6.20,9.10)) 6.20 6.35 $C.GrayLine $true 1.45),
        (E 'newPlan' 'newValid' '是' 'left' 'top' @(@(2.45,2.47),@(2.45,1.80),@(1.25,1.80)) 2.55 2.47 $C.BlueLine),
        (E 'newPlan' 'existingValid' '否：同planId' 'right' 'top' @(@(5.75,2.47),@(5.75,1.80),@(7.15,1.80)) 5.75 2.72 $C.BlueLine $false 1.35),
        (E 'newValid' 'completion' '新Plan合法' 'right' 'left' @(@(2.65,1.35),@(2.65,9.35),@(8.85,9.35)) 5.75 9.35 $C.GrayLine $true 1.25),
        (E 'existingValid' 'completion' '合同合法' 'right' 'left' @(@(8.75,1.35),@(8.75,9.10)) 8.75 5.20 $C.GrayLine $true 1.15),
        (E 'newValid' 'rejectPlan' '否' 'bottom' 'left' @(@(1.25,0.62)) 1.25 0.94 $C.RedLine),
        (E 'existingValid' 'rejectPlan' '否' 'bottom' 'right' @(@(7.15,0.62)) 7.15 0.94 $C.RedLine),
        (E 'completion' 'build' '是'),
        (E 'completion' 'rejectPlan' '否' 'left' 'right' @(@(8.20,9.10),@(8.20,0.62)) 8.20 4.70 $C.RedLine),
        (E 'build' 'save'),
        (E 'save' 'end'),
        (E 'existingValid' 'immutability' '合同检查' 'right' 'left' @(@(8.75,1.35),@(8.75,3.05)) 8.75 2.20 $C.RedLine),
        (E 'immutability' 'replan' '需要改路线' 'bottom' 'top' @() 11.35 2.38 $C.RedLine $true 1.30)
    )
    Add-PageFlow $page $masters $n $e
    Add-PlainText $page 14.70 5.25 3.05 0.36 '职责对比（不是同一个revision字段）' 7.0 $true $C.Navy 'center' $C.White $false | Out-Null

    $document.SaveAs($vsdxPath)

    $previewNames = @(
        '01-run-agent-production-loop.png',
        '02-harness-control-flow.png',
        '03-planner-detailed-flow.png',
        '04-executor-single-step-flow.png',
        '05-validator-flow.png',
        '06-replanner-recovery-flow.png',
        '07-taskstate-patch-occ-flow.png'
    )
    for ($i = 1; $i -le $document.Pages.Count; $i++) {
        $document.Pages.Item($i).Export((Join-Path $previewRoot $previewNames[$i - 1]))
    }

    $document.ExportAsFixedFormat(1, $pdfPath, 1, 0)

    Write-Output "VSDX=$vsdxPath"
    Write-Output "PDF=$pdfPath"
    Write-Output "PREVIEW=$previewRoot"
    Write-Output "PAGES=$($document.Pages.Count)"
}
finally {
    if ($document -ne $null) {
        try { $document.Close() } catch {}
        try { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($document) } catch {}
    }
    if ($stencil -ne $null) {
        try { $stencil.Close() } catch {}
        try { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($stencil) } catch {}
    }
    if ($visio -ne $null) {
        try { $visio.Quit() } catch {}
        try { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($visio) } catch {}
    }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}
