[CmdletBinding()]
param([ValidateSet('prepare','start','health','stop','freeze-agent','restart-agent','restart-search')][string]$Action='health', [switch]$DisableCatalogSearch, [switch]$ExactCatalogSearch, [ValidateSet(1,2,3)][int]$CatalogIndexVersion=3)
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$runtime=Join-Path $root '.runtime/merged-commerce'
$python=Join-Path $root '.venv/Scripts/python.exe'
$stateFile=Join-Path $runtime 'processes.json'
function Docker([string[]]$a) { & docker.exe @a; if($LASTEXITCODE -ne 0){throw 'Docker operation failed'} }
function WaitHttp($url,[int]$TimeoutSeconds=90) {
    $deadline=[datetime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {try{return Invoke-RestMethod $url -TimeoutSec 3}catch{Start-Sleep -Milliseconds 500}}while([datetime]::UtcNow -lt $deadline)
    throw "Not ready: $url"
}
function ReadState { if(Test-Path $stateFile){return Get-Content $stateFile -Raw -Encoding utf8|ConvertFrom-Json -AsHashtable}; return @{} }
function SaveState($s) { $s|ConvertTo-Json -Depth 4|Set-Content $stateFile -Encoding utf8 }
function ReadCatalogManifest {
    if(!$microRelease){return Invoke-RestMethod 'http://127.0.0.1:8080/internal/catalog/manifest' -TimeoutSec 5}
    $catalogToken=(Get-Content (Join-Path $runtime 'secrets/catalog-internal.token') -Raw).Trim()
    foreach($origin in $microRelease.catalogUrls){
        try{return Invoke-RestMethod ($origin+'/internal/catalog/manifest') -Headers @{'X-Internal-Service-Token'=$catalogToken} -TimeoutSec 5}catch{}
    }
    throw 'No selected internal catalog instance returned its manifest'
}
function Owned($record) {
    if(!$record){return $null}
    $p=Get-CimInstance Win32_Process -Filter "ProcessId=$($record.pid)"
    if(!$p){return $null}
    if($p.CreationDate.ToUniversalTime() -ne ([datetime]$record.createdAtUtc).ToUniversalTime() -or !$p.CommandLine.Contains($record.marker)){
        throw 'Process identity changed; refusing to stop/reuse it'
    }
    return $p
}
function StopOwnedWithWorker($record) {
    $parent=Owned $record
    if(!$parent){return}
    # Capture only this owned Python process's multiprocessing worker, not other Python jobs.
    $workers=@(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($parent.ProcessId)" | Where-Object {
        $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine.Contains("spawn_main(parent_pid=$($parent.ProcessId),") -and
        $_.CommandLine.Contains('--multiprocessing-fork') -and $_.CreationDate -ge $parent.CreationDate
    })
    Stop-Process -Id $parent.ProcessId -Force
    foreach($worker in $workers){
        $current=Get-CimInstance Win32_Process -Filter "ProcessId=$($worker.ProcessId)"
        if($current -and $current.CreationDate -eq $worker.CreationDate -and $current.CommandLine -eq $worker.CommandLine){Stop-Process -Id $current.ProcessId -Force}
    }
}
function StartOwned($name,$exe,$arguments,$marker,$port,$url,$s) {
    $listeners=@(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
    if($listeners.Count){
        $owned=Owned $s[$name]
        if($owned -and $owned.ProcessId -notin $listeners.OwningProcess){
            # A timed-out launcher may have recorded the venv wrapper before
            # its verified child opened the port. Adopt only that exact child.
            $children=@($listeners | ForEach-Object {Get-CimInstance Win32_Process -Filter "ProcessId=$($_.OwningProcess)"} | Where-Object {$_.ParentProcessId -eq $owned.ProcessId -and $_.CommandLine.Contains($marker)})
            if($children.Count -ne 1){throw "Port $port is occupied by an unowned process"}
            $owned=$children[0]
            $s[$name]=@{pid=$owned.ProcessId;createdAtUtc=$owned.CreationDate.ToUniversalTime().ToString('o');marker=$marker}
            SaveState $s
        }
        if(!$owned -or $owned.ProcessId -notin $listeners.OwningProcess){throw "Port $port is occupied by an unowned process"}
        return
    }
    $p=Start-Process $exe -ArgumentList $arguments -WorkingDirectory $root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $runtime "$name.stdout.log") -RedirectStandardError (Join-Path $runtime "$name.stderr.log")
    # Record launcher immediately so interrupted startup remains recoverable.
    $proc=Get-CimInstance Win32_Process -Filter "ProcessId=$($p.Id)"
    $s[$name]=@{pid=$p.Id;createdAtUtc=$proc.CreationDate.ToUniversalTime().ToString('o');marker=$marker}
    SaveState $s
    if($url){WaitHttp $url -TimeoutSeconds $(if($name -in @('agent','search')){600}else{90})|Out-Null}else{
        # Knowledge startup may exceed three seconds after a cold process start.
        # Wait for the actual listener instead of treating a slow start as failure.
        $portDeadline=[datetime]::UtcNow.AddSeconds(45)
        while(!(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)){
            if([datetime]::UtcNow -ge $portDeadline){throw "Port $port did not become ready"}
            Start-Sleep -Milliseconds 500
        }
    }
    $listener=Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction Stop|Select-Object -First 1
    $proc=Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
    if(!$proc.CommandLine.Contains($marker) -or ($proc.ProcessId -ne $p.Id -and $proc.ParentProcessId -ne $p.Id)){throw 'Unexpected child process'}
    $s[$name]=@{pid=$proc.ProcessId;createdAtUtc=$proc.CreationDate.ToUniversalTime().ToString('o');marker=$marker}
    SaveState $s
}
New-Item -ItemType Directory -Force $runtime,(Join-Path $runtime 'secrets'),(Join-Path $runtime 'warehouse')|Out-Null
$secret=Join-Path $runtime 'secrets/warehouse.token'
if(!(Test-Path $secret)) { [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))|Set-Content $secret -Encoding ascii }
$env:MERGED_WAREHOUSE_TOKEN=(Get-Content $secret -Raw).Trim()
$observerSecret=Join-Path $runtime 'secrets/observer.token'
if(!(Test-Path $observerSecret)){throw 'Observer server secret missing; provision it locally before starting'}
$env:MERGED_OBSERVER_KEY=(Get-Content $observerSecret -Raw).Trim()
$compose=@('compose','-f',(Join-Path $root 'docker-compose.yml'),'-f',(Join-Path $root 'compose.merged-commerce.yml'))
$s=ReadState
$fullCatalogRelease=Test-Path (Join-Path $runtime 'full-catalog-release.json')
$microReleaseFile=Join-Path $runtime 'microservices-release.json'
$microRelease=if(Test-Path $microReleaseFile){Get-Content $microReleaseFile -Raw -Encoding utf8|ConvertFrom-Json}else{$null}
$searchReleaseFile=Join-Path $runtime 'search-service-release.json'
$searchRelease=if(Test-Path $searchReleaseFile){Get-Content $searchReleaseFile -Raw -Encoding utf8|ConvertFrom-Json}else{$null}
if($searchRelease -and ($searchRelease.protocol -ne 'catalog.search.v1' -or $searchRelease.url -ne 'http://127.0.0.1:18110')){throw 'Unexpected independent search release binding'}
if($Action -eq 'restart-agent') {
    StopOwnedWithWorker $s['agent']
    $Action='start'
}
if($Action -eq 'freeze-agent') {
    StopOwnedWithWorker $s['agent']
    Write-Output 'Agent writer stopped; frontend, independent search and stored data preserved.'
    exit
}
if($Action -eq 'restart-search') {
    if(!$searchRelease){throw 'Independent search release is not configured'}
    StopOwnedWithWorker $s['search']
    $Action='start'
}
if($Action -eq 'prepare') {
    if($fullCatalogRelease){throw 'Full catalog release selected; use start to preserve the validated database binding'}
    Docker ($compose+@('up','-d','--no-build','rabbitmq','nameserver','rocketmq'))
    Docker ($compose+@('up','--no-build','--no-deps','--exit-code-from','rocket-topics','rocket-topics'))
    Docker ($compose+@('up','-d','--no-build','--no-deps','backend','warehouse-simulator'))
    WaitHttp 'http://127.0.0.1:8080/actuator/health/readiness'|Out-Null
    Write-Output 'Java and local warehouse ready; public entry still stopped.'
    exit
}
if($Action -eq 'stop') {
    foreach($name in @('frontend','agent','search')) { StopOwnedWithWorker $s[$name] }
    Write-Output 'Public entry stopped; databases, receipts and shared knowledge preserved.'
    exit
}
if($Action -eq 'start') {
    if(Test-Path (Join-Path $runtime 'microservices-maintenance.json')){throw 'Data migration window active; use the guarded release procedure before restarting the public Agent'}
    Docker ($compose+@('up','-d','--no-build','rabbitmq','nameserver','rocketmq'))
    Docker ($compose+@('up','--no-build','--no-deps','--exit-code-from','rocket-topics','rocket-topics'))
    $containers=if($fullCatalogRelease){@('local-life-redis','local-life-kafka','local-life-elasticsearch','local-life-warehouse-simulator')}else{@('local-life-mysql','local-life-redis','local-life-kafka','local-life-elasticsearch','local-life-warehouse-simulator','local-life-backend')}
    foreach($name in $containers) {
        $container=((docker.exe inspect $name|ConvertFrom-Json)[0])
        if($LASTEXITCODE -ne 0){throw "Existing deployment container missing: $name"}
        if(!$container.State.Running){Docker @('start',$name)|Out-Null}
    }
    if($microRelease){
        & $python -X utf8 (Join-Path $root 'scripts/microservices-release-20260915/live_release.py') ensure-backend
        if($LASTEXITCODE -ne 0){throw 'Microservices release binding failed; monolith fallback is forbidden'}
    }elseif($fullCatalogRelease){
        & $python -X utf8 (Join-Path $root 'scripts/commerce-full-release-20260915/release.py') ensure-backend
        if($LASTEXITCODE -ne 0){throw 'Full catalog backend binding failed'}
    }else{Docker ($compose+@('up','-d','--no-build','--no-deps','backend','warehouse-simulator'))}
    WaitHttp 'http://127.0.0.1:8080/actuator/health/readiness'|Out-Null
    $old=((docker.exe inspect local-life-agent|ConvertFrom-Json)[0])
    if($old.State.Running){throw 'Old Docker Agent is running. Refusing two authorities on the daily entry.'}
    $manifest=ReadCatalogManifest
    if($manifest.data.catalogVersion -ne 'merged-used-phone-439-20260909-v1' -or $manifest.data.productCount -ne 439){throw 'Catalog migration not validated'}
    if($microRelease){
        & $python -X utf8 (Join-Path $root 'scripts/microservices-release-20260915/verify_read_dependencies.py')
        if($LASTEXITCODE -ne 0){throw 'Product/inventory read readiness failed; refusing to announce a usable storefront'}
    }
    # The exact catalog contract is checked in health below; do not seed in this launcher.
    $env:BACKEND_BASE_URL='http://127.0.0.1:8080'
    $env:BACKEND_OBSERVER_ENABLED='true'
    $env:BACKEND_OBSERVER_KEY=$env:MERGED_OBSERVER_KEY
    $env:BACKEND_OBSERVER_INSTANCES=if($microRelease){$microRelease.observerInstances|ConvertTo-Json -Compress}else{'{}'}
    $env:CATALOG_INTERNAL_BASE_URLS=if($microRelease){ConvertTo-Json -InputObject @($microRelease.catalogUrls) -Compress}else{'[]'}
    $env:CATALOG_INTERNAL_TOKEN_FILE=if($microRelease){Join-Path $runtime 'secrets/catalog-internal.token'}else{''}
    # This Windows host has a native IPv4 Redis on 6379 as well. Pin Docker's
    # IPv6 mapping and compare server identity, never guess from the port alone.
    $env:REDIS_URL='redis://[::1]:6379/0'
    $dockerRedis=((docker.exe exec local-life-redis redis-cli INFO server | Select-String '^run_id:').Line -split ':',2)[1].Trim()
    $agentRedis=(& $python -c "import os,redis; print(redis.Redis.from_url(os.environ['REDIS_URL']).info('server')['run_id'])").Trim()
    if($LASTEXITCODE -ne 0 -or $dockerRedis -ne $agentRedis){throw 'Agent Redis differs from Java Docker Redis'}
    $env:AGENT_TRANSACTION_ENABLED='true'
    $env:COMMERCE_DEMO_ENABLED='true'
    # Public documents have their own read-only workflow; phone commerce stays intact.
    # Rollback: restart-agent -DisableCatalogSearch.
    $env:CATALOG_WORKSPACE_ENABLED=if($DisableCatalogSearch){'false'}else{'true'}
    $env:CATALOG_WORKSPACE_FAST_ENABLED=if($ExactCatalogSearch){'false'}else{'true'}
    $env:CATALOG_WORKSPACE_FAST_INDEX_VERSION=[string]$CatalogIndexVersion
    $env:CATALOG_WORKSPACE_REUSE_MODEL_CLIENT='true'
    $env:CATALOG_SEARCH_SERVICE_URL=if($searchRelease){$searchRelease.url}else{''}
    $env:CATALOG_SEARCH_TOKEN_FILE=if($searchRelease){Join-Path $runtime 'secrets/catalog-search.token'}else{''}
    $env:COMMERCE_DEMO_PAYMENT_SIMULATION_ENABLED='true'
    $env:COMMERCE_WORKSPACE_CART_ENABLED='true'
    $env:COMMERCE_WORKSPACE_LOCAL_OFFERS_ENABLED='true'
    $env:COMMERCE_WORKSPACE_EXTERNAL_CATALOG_ENABLED=if($fullCatalogRelease){'true'}else{'false'}
    $env:PRODUCT_LEGACY_CATALOG_VERSION=if($fullCatalogRelease){'merged-used-phone-439-20260909-v1'}else{''}
    $env:COMMERCE_WORKSPACE_EPOCH='merged439-v1'
    $env:PRODUCT_RETRIEVAL_MODE='hybrid'
    $env:PRODUCT_VECTOR_BACKEND='local'
    $env:PRODUCT_VECTOR_TIMEOUT_SECONDS='30'
    $env:RAG_MODEL_CACHE_DIR=Join-Path $root 'agent/.cache/fastembed'
    $env:USED_PHONE_SYNTHETIC_PRICE_POLICY='budget_and_ranking'
    $env:USED_PHONE_SYNTHETIC_PRICE_DIR=Join-Path $root 'datasets/current/used-phone'
    $env:PRODUCT_KNOWLEDGE_ENABLED='true'
    $env:PRODUCT_KNOWLEDGE_MCP_URL='http://127.0.0.1:18791/mcp'
    $env:PRODUCT_KNOWLEDGE_TIMEOUT_SECONDS='10'
    # Match the production decision runtime; pause/recovery uses shared durable boundaries.
    $env:AGENT_CONTROL_RUNTIME='react_v1'
    $env:AGENT_REACT_LIVE_ENABLED='true'
    $env:AGENT_GRAPH_V2_DURABLE_ENABLED='true'
    $env:SHOPPING_STATE_AUTHORITY='v2'
    $env:AGENT_ORCHESTRATOR_MODE='unified'
    $env:AGENT_LEGACY_FALLBACK_ENABLED='false'
    $env:AGENT_FINAL_ANSWER_THINKING='disabled'
    $env:AGENT_FINAL_ANSWER_MAX_TOKENS='800'
    $env:PYTHONPATH=$root
    if(!(Get-NetTCPConnection -State Listen -LocalPort 18791 -ErrorAction SilentlyContinue)) {
        StartOwned 'knowledge' $python @('-m','agent.app.product_knowledge.server') 'agent.app.product_knowledge.server' 18791 $null $s
    }
    Push-Location $root
    try {
        & $python -B -m agent.app.product_knowledge.health
        if($LASTEXITCODE -ne 0){throw 'Knowledge service not ready'}
    } finally { Pop-Location }
    if($searchRelease){
        if(!(Test-Path $env:CATALOG_SEARCH_TOKEN_FILE)){throw 'Independent search credential missing'}
        StartOwned 'search' $python @('-m','uvicorn','agent.app.catalog_search_server:app','--host','127.0.0.1','--port','18110') 'agent.app.catalog_search_server:app' 18110 'http://127.0.0.1:18110/health' $s
    }
    StartOwned 'agent' $python @('-m','uvicorn','agent.app.main:app','--host','127.0.0.1','--port','8000') 'agent.app.main:app' 8000 'http://127.0.0.1:8000/health' $s
    $env:COMMERCE_BFF_URL='http://127.0.0.1:8000'
    $vite=Join-Path $root 'frontend/node_modules/vite/bin/vite.js'
    StartOwned 'frontend' (Get-Command node).Source @("`"$vite`"",(Join-Path $root 'frontend'),'--host','127.0.0.1','--port','5173','--strictPort','--config',(Join-Path $root 'frontend/vite.config.ts')) $vite 5173 'http://127.0.0.1:5173/' $s
}
$agent=Invoke-RestMethod 'http://127.0.0.1:8000/health' -TimeoutSec 5
$cap=Invoke-RestMethod 'http://127.0.0.1:5173/api/commerce-demo/capability' -TimeoutSec 5
if($agent.backendBaseUrl -ne 'http://127.0.0.1:8080' -or !$cap.enabled -or !$cap.paymentSimulationEnabled){throw 'Unified health contract failed'}
$java=Invoke-RestMethod 'http://127.0.0.1:8080/actuator/health/readiness' -TimeoutSec 5
$catalog=ReadCatalogManifest
foreach($name in @('agent','frontend')) { if(!(Owned $s[$name])){throw "Owned $name process is missing"} }
if($searchRelease){
    if(!(Owned $s['search'])){throw 'Owned independent search process is missing'}
    $searchHealth=Invoke-RestMethod ($searchRelease.url+'/health') -TimeoutSec 5
    if($searchHealth.protocol -ne 'catalog.search.v1' -or $searchHealth.status -ne 'UP'){throw 'Independent search health contract failed'}
}
if($java.status -ne 'UP' -or $catalog.data.productCount -ne 439 -or $catalog.data.catalogVersion -ne 'merged-used-phone-439-20260909-v1'){throw 'Java/catalog contract failed'}
@{status='ok';frontend='http://127.0.0.1:5173/';agent='8000';java='8080';catalog=$(if($fullCatalogRelease){'external-full-with-legacy-phone-439'}else{'used-phone-439'});payment='local_only'}|ConvertTo-Json
