$ErrorActionPreference = 'Continue'
$backupRoot = 'F:\agent\crash-backup-20260820'
New-Item -ItemType Directory -LiteralPath $backupRoot -Force | Out-Null

Start-Transcript -LiteralPath (Join-Path $backupRoot 'repair-admin.log') -Force
Write-Host '=== Backup crash artifacts ==='
$crashFiles = @(
    'C:\Windows\MEMORY.DMP',
    'C:\Windows\Minidump\082026-77312-01.dmp',
    'C:\Windows\Minidump\081726-104484-01.dmp',
    'C:\Windows\LiveKernelReports\WATCHDOG\WATCHDOG-20260820-0432.dmp',
    'C:\Windows\LiveKernelReports\WATCHDOG\WATCHDOG-20260820-0433.dmp',
    'C:\Windows\LiveKernelReports\WATCHDOG\WATCHDOG-20260820-0437.dmp'
)
foreach ($sourcePath in $crashFiles) {
    if (Test-Path -LiteralPath $sourcePath) {
        Copy-Item -LiteralPath $sourcePath -Destination $backupRoot -Force
        Write-Host "Copied $sourcePath"
    } else {
        Write-Host "Not found $sourcePath"
    }
}

wevtutil epl System (Join-Path $backupRoot 'System.evtx') /ow:true
wevtutil epl Application (Join-Path $backupRoot 'Application.evtx') /ow:true
reg export 'HKLM\SYSTEM\CurrentControlSet\Services\netrtp' (Join-Path $backupRoot 'netrtp-service.reg') /y
reg export 'HKLM\SYSTEM\CurrentControlSet\Control\GraphicsDrivers' (Join-Path $backupRoot 'graphicsdrivers.reg') /y
powercfg /export (Join-Path $backupRoot 'balanced-power-plan.pow') 381b4222-f694-41f0-9685-ff5bb260df2e

Write-Host '=== Disable the 0x139 suspect driver ==='
sc.exe stop netrtp
sc.exe config netrtp start= disabled

Write-Host '=== Pause leak-like background components ==='
Stop-Process -Name LeSearch -Force -ErrorAction SilentlyContinue
sc.exe stop LeCloudService
sc.exe config LeCloudService start= disabled
Stop-Process -Name wallpaper32,wallpaper64,wallpaperui -Force -ErrorAction SilentlyContinue
sc.exe stop 'Wallpaper Engine Service'
sc.exe config 'Wallpaper Engine Service' start= disabled

Write-Host '=== Stabilize display and S3 power transitions for A/B testing ==='
powercfg /change monitor-timeout-ac 0
powercfg /change monitor-timeout-dc 0
powercfg /change standby-timeout-ac 0
powercfg /change standby-timeout-dc 0
powercfg /change hibernate-timeout-ac 0
powercfg /change hibernate-timeout-dc 0
powercfg /setactive 381b4222-f694-41f0-9685-ff5bb260df2e

Write-Host '=== Result ==='
sc.exe query netrtp
Get-Service -Name LeCloudService,'Wallpaper Engine Service' | Select-Object Name,Status,StartType | Format-Table -AutoSize
powercfg /query scheme_current sub_sleep standbyidle
powercfg /query scheme_current sub_sleep hibernateidle
powercfg /query scheme_current sub_video videoidle
Stop-Transcript
