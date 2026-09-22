$ErrorActionPreference = 'Stop'

$source = 'C:\Windows\MEMORY.DMP'
$destinationRoot = 'F:\agent\crash-backup-20260826'
$dump = Join-Path $destinationRoot 'MEMORY-20260826.dmp'
$analysis = Join-Path $destinationRoot 'analysis-20260826.txt'
$cdb = 'C:\Program Files\WindowsApps\Microsoft.WinDbg_1.2606.22001.0_x64__8wekyb3d8bbwe\amd64\cdb.exe'

New-Item -ItemType Directory -Path $destinationRoot -Force | Out-Null
Copy-Item -LiteralPath $source -Destination $dump -Force

$commands = '.symfix F:\agent\symbols; .reload; !analyze -v; !vm 1; lmvm nvlddmkm; !blackboxbsd; q'
& $cdb -z $dump -logo $analysis -c $commands
exit $LASTEXITCODE
