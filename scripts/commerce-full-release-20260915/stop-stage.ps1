$ErrorActionPreference='Stop'
$out='D:/agent-datasets/commerce-full-release-20260915-attempt001'
$state=Get-Content "$out/stage-processes.json" -Raw|ConvertFrom-Json -AsHashtable
function StopOwnedTree($proc) {
    foreach($child in @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($proc.ProcessId)")){StopOwnedTree $child}
    $now=Get-CimInstance Win32_Process -Filter "ProcessId=$($proc.ProcessId)"
    if($now -and $now.CreationDate -eq $proc.CreationDate){
        try {Stop-Process -Id $proc.ProcessId -Force}
        catch {if(Get-Process -Id $proc.ProcessId -ErrorAction SilentlyContinue){throw}}
    }
}
foreach($name in @('stage-frontend','stage-agent','stage-knowledge')){
    $r=$state[$name];if(!$r){continue}
    $p=Get-CimInstance Win32_Process -Filter "ProcessId=$($r.pid)"
    if(!$p){continue}
    if($p.CreationDate.ToUniversalTime() -ne ([datetime]$r.createdAtUtc).ToUniversalTime() -or !$p.CommandLine.Contains($r.marker)){throw "Ownership changed: $name"}
    StopOwnedTree $p
}
foreach($name in @('java','warehouse','kafka','rabbit','redis')){
    $container="commerce-full-stage-$name-20260915"
    $i=docker inspect $container|ConvertFrom-Json
    if($LASTEXITCODE -ne 0 -or $i.Config.Labels.'commerce.release' -ne '20260915-a1'){throw "Container ownership changed: $container"}
    if($i.State.Running){docker stop -t 30 $container;if($LASTEXITCODE -ne 0){throw "Stop failed: $container"}}
}
'Only owned acceptance services stopped. Candidate import, original data and evidence preserved.'
