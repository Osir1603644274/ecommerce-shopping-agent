$ErrorActionPreference = 'Continue'

Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'CodexStabilityMonitor' -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName 'Codex Stability Monitor' -Confirm:$false -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -like '*F:\agent\memory-watch.ps1*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

sc.exe config netrtp start= demand
sc.exe config LeCloudService start= auto
sc.exe start LeCloudService
sc.exe config 'Wallpaper Engine Service' start= auto
sc.exe start 'Wallpaper Engine Service'

# Restore the settings recorded immediately before repair.
powercfg /change standby-timeout-ac 0
powercfg /change standby-timeout-dc 180
powercfg /change hibernate-timeout-ac 0
powercfg /change hibernate-timeout-dc 180
powercfg /change monitor-timeout-ac 60
powercfg /change monitor-timeout-dc 3
powercfg /setactive 381b4222-f694-41f0-9685-ff5bb260df2e

Write-Host 'Repair changes have been reverted.'
