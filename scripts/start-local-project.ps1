# Double-click entry point for the existing, selected local deployment.
# Never imports data, builds images, or changes which release is selected.
[CmdletBinding()]
param([switch]$CheckOnly, [switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $PSScriptRoot 'merged-commerce.ps1'
$mutex = $null
$ownsMutex = $false

function Test-DockerEngine {
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = (Get-Command docker.exe -ErrorAction Stop).Source
    $info.Arguments = 'info --format {{.ServerVersion}}'
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $probe = [Diagnostics.Process]::Start($info)
    try {
        if (!$probe.WaitForExit(5000)) {
            # Only terminate the diagnostic CLI child we just created.
            $probe.Kill()
            return $false
        }
        return $probe.ExitCode -eq 0
    } finally { $probe.Dispose() }
}

try {
    if ($PSVersionTable.PSVersion.Major -lt 7) { throw '请通过 start-project.bat 启动，需要 PowerShell 7。' }
    foreach ($path in @($launcher, (Join-Path $projectRoot '.venv/Scripts/python.exe'),
            (Join-Path $projectRoot 'frontend/node_modules/vite/bin/vite.js'),
            (Join-Path $projectRoot '.runtime/merged-commerce/secrets/observer.token'))) {
        if (!(Test-Path -LiteralPath $path -PathType Leaf)) { throw "启动所需文件缺失：$path" }
    }
    foreach ($command in @('docker.exe', 'node.exe')) { Get-Command $command -ErrorAction Stop | Out-Null }
    if ($CheckOnly) {
        Write-Host '启动入口检查通过。未启动 Docker、项目服务或浏览器。'
        exit 0
    }
    $nameHash = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData(
        [Text.Encoding]::UTF8.GetBytes($projectRoot.ToLowerInvariant()))).Substring(0, 16)
    $mutex = [Threading.Mutex]::new($false, "Local\ShoppingAgentStartup-$nameHash")
    try { $ownsMutex = $mutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $ownsMutex = $true }
    if (!$ownsMutex) { throw '另一个一键启动窗口正在工作，请等待它完成，不要重复点击。' }

    Set-Location -LiteralPath $projectRoot
    Write-Host '[1/3] 检查 Docker…'
    if (!(Test-DockerEngine)) {
        $desktop = Join-Path $env:ProgramFiles 'Docker/Docker/Docker Desktop.exe'
        if (!(Test-Path -LiteralPath $desktop)) { throw '未找到 Docker Desktop，请手动打开它后重试。' }
        if (!(Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue)) {
            Start-Process -FilePath $desktop -WindowStyle Hidden | Out-Null
        }
        Write-Host '等待 Docker 就绪，首次启动可能需要几分钟…'
        $deadline = [DateTime]::UtcNow.AddMinutes(3)
        while (!(Test-DockerEngine)) {
            if ([DateTime]::UtcNow -ge $deadline) { throw 'Docker 未能就绪。请检查 Docker Desktop 是否需要登录、确认或修复。' }
            Start-Sleep -Seconds 2
        }
    }
    Write-Host '[2/3] 启动现有数据库、中间件、Java 服务、知识库、搜索、Agent 和前端…'
    Write-Host '复用已配置的部署和数据；冷启动加载搜索索引可能需要数分钟。'
    & $launcher -Action start
    # The selected launcher throws if any of its health/ownership checks fail.
    Write-Host '[3/3] 启动检查通过：http://127.0.0.1:5173/' -ForegroundColor Green
    if (!$NoBrowser) { Start-Process 'http://127.0.0.1:5173/' | Out-Null }
    exit 0
} catch {
    Write-Host ("启动未完成：" + $_.Exception.Message) -ForegroundColor Red
    Write-Host '不要清理数据库或重复强制重启。可将本窗口报错发给我检查。'
    Write-Host '服务日志目录：.runtime/merged-commerce/'
    exit 1
} finally {
    if ($ownsMutex) { $mutex.ReleaseMutex() }
    if ($null -ne $mutex) { $mutex.Dispose() }
}
