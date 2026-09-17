$ErrorActionPreference='Stop'
$root='F:/agent'
$state=Get-Content "$root/.runtime/merged-commerce/processes.json" -Raw|ConvertFrom-Json
$record=$state.search
$process=Get-CimInstance Win32_Process -Filter "ProcessId=$($record.pid)"
if(!$process -or $process.CreationDate.ToUniversalTime() -ne ([datetime]$record.createdAtUtc).ToUniversalTime() -or !$process.CommandLine.Contains('agent.app.catalog_search_server:app')){throw 'Search process identity changed'}
$workers=@(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($process.ProcessId)"|Where-Object {$_.Name -eq 'python.exe' -and $_.CommandLine.Contains("spawn_main(parent_pid=$($process.ProcessId),")})
try {
    Stop-Process -Id $process.ProcessId -Force
    foreach($worker in $workers){
        $current=Get-CimInstance Win32_Process -Filter "ProcessId=$($worker.ProcessId)"
        if($current -and $current.CreationDate -eq $worker.CreationDate -and $current.CommandLine -eq $worker.CommandLine){Stop-Process -Id $current.ProcessId -Force}
    }
    & "$root/.venv/Scripts/python.exe" "$root/scripts/microservices-release-20260915/verify_search_outage.py"
    if($LASTEXITCODE -ne 0){throw 'Search outage acceptance failed'}
} finally {
    & "$root/scripts/merged-commerce.ps1" -Action restart-search
    if($LASTEXITCODE -ne 0){throw 'Search restart failed'}
}
