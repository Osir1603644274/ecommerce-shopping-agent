param([ValidateSet('all','restart-agent','stop-agent','start-agent')][string]$Action='all')
$ErrorActionPreference='Stop'
$root='F:/agent'
$out='D:/agent-datasets/commerce-full-release-20260915-attempt001'
$ports=Get-Content "$out/stage-ports.json" -Raw|ConvertFrom-Json
$env:BACKEND_BASE_URL='http://127.0.0.1:19380'
$env:REDIS_URL="redis://127.0.0.1:$($ports.redis)/0"
$env:BACKEND_OBSERVER_ENABLED='true'
$env:BACKEND_OBSERVER_KEY=(Get-Content "$root/.runtime/merged-commerce/secrets/observer.token" -Raw).Trim()
$env:AGENT_TRANSACTION_ENABLED='true'
$env:COMMERCE_DEMO_ENABLED='true'
$env:COMMERCE_DEMO_PAYMENT_SIMULATION_ENABLED='true'
$env:COMMERCE_WORKSPACE_CART_ENABLED='true'
$env:COMMERCE_WORKSPACE_LOCAL_OFFERS_ENABLED='true'
$env:COMMERCE_WORKSPACE_EXTERNAL_CATALOG_ENABLED='true'
$env:COMMERCE_WORKSPACE_EPOCH='full-stage-20260915'
$env:CATALOG_WORKSPACE_ENABLED='true'
$env:CATALOG_WORKSPACE_FAST_ENABLED='true'
$env:CATALOG_WORKSPACE_FAST_INDEX_VERSION='3'
$env:CATALOG_WORKSPACE_REUSE_MODEL_CLIENT='true'
$env:PRODUCT_RETRIEVAL_MODE='hybrid'
$env:PRODUCT_LEGACY_CATALOG_VERSION='merged-used-phone-439-20260909-v1'
$env:PRODUCT_VECTOR_BACKEND='local'
$env:PRODUCT_VECTOR_TIMEOUT_SECONDS='30'
$env:RAG_MODEL_CACHE_DIR="$root/agent/.cache/fastembed"
$env:USED_PHONE_SYNTHETIC_PRICE_POLICY='budget_and_ranking'
$env:USED_PHONE_SYNTHETIC_PRICE_DIR="$root/datasets/current/used-phone"
$env:PRODUCT_KNOWLEDGE_ENABLED='true'
$env:PRODUCT_KNOWLEDGE_MCP_URL='http://127.0.0.1:18792/mcp'
$env:PRODUCT_KNOWLEDGE_TIMEOUT_SECONDS='10'
$env:AGENT_CONTROL_RUNTIME='fixed_v1'
$env:AGENT_REACT_LIVE_ENABLED='false'
$env:AGENT_GRAPH_V2_DURABLE_ENABLED='true'
$env:SHOPPING_STATE_AUTHORITY='v2'
$env:AGENT_ORCHESTRATOR_MODE='unified'
$env:AGENT_LEGACY_FALLBACK_ENABLED='false'
$env:AGENT_FINAL_ANSWER_THINKING='disabled'
$env:AGENT_FINAL_ANSWER_MAX_TOKENS='800'
$env:WEB_QUERY_INTAKE_ENABLED='true'
$env:WEB_QUERY_INTAKE_PATH="$out/stage-queries.sqlite3"
$env:REVIEW_PROJECTION_RECEIPTS_PATH="$out/stage-review-receipts.sqlite3"
$env:MODEL_CALL_RECEIPT_SPOOL_PATH="$out/stage-model-receipts.jsonl"
$env:CONTEXT_RECEIPT_SPOOL_PATH="$out/stage-context-receipts.jsonl"
$env:PYTHONPATH=$root
$env:COMMERCE_BFF_URL='http://127.0.0.1:8001'
$env:VITE_CACHE_DIR="$out/vite-cache"
$state=if(Test-Path "$out/stage-processes.json"){Get-Content "$out/stage-processes.json" -Raw|ConvertFrom-Json -AsHashtable}else{@{}}
if($Action -in @('restart-agent','stop-agent')) {
    $record=$state['stage-agent']
    $owned=Get-CimInstance Win32_Process -Filter "ProcessId=$($record.pid)"
    if(!$owned -or $owned.CreationDate.ToUniversalTime() -ne ([datetime]$record.createdAtUtc).ToUniversalTime() -or !$owned.CommandLine.Contains($record.marker)){throw 'Stage Agent ownership changed'}
    function StopOwnedTree($proc) {
        foreach($child in @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($proc.ProcessId)")){StopOwnedTree $child}
        $now=Get-CimInstance Win32_Process -Filter "ProcessId=$($proc.ProcessId)"
        if($now -and $now.CreationDate -eq $proc.CreationDate){Stop-Process -Id $proc.ProcessId -Force}
    }
    StopOwnedTree $owned
}
if($Action -eq 'stop-agent'){Write-Output 'Owned acceptance Agent stopped; importer and data preserved.';exit}
function Launch($name,$exe,$arguments,$port,$marker) {
    if(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue){throw "Port $port occupied; inspect before resuming"}
    $p=Start-Process $exe -ArgumentList $arguments -WorkingDirectory $root -WindowStyle Hidden -PassThru -RedirectStandardOutput "$out/$name.stdout.log" -RedirectStandardError "$out/$name.stderr.log"
    $proc=Get-CimInstance Win32_Process -Filter "ProcessId=$($p.Id)"
    $state[$name]=@{pid=$p.Id;createdAtUtc=$proc.CreationDate.ToUniversalTime().ToString('o');marker=$marker;port=$port}
    $state|ConvertTo-Json -Depth 5|Set-Content "$out/stage-processes.json" -Encoding utf8
}
if($Action -eq 'all'){Launch 'stage-knowledge' "$root/.venv/Scripts/python.exe" @('-m','agent.app.product_knowledge.server','--port','18792') 18792 'agent.app.product_knowledge.server'}
Launch 'stage-agent' "$root/.venv/Scripts/python.exe" @('-m','uvicorn','agent.app.main:app','--host','127.0.0.1','--port','8001') 8001 'agent.app.main:app'
if($Action -eq 'all'){Launch 'stage-frontend' (Get-Command node).Source @("$root/frontend/node_modules/vite/bin/vite.js","$root/frontend",'--host','127.0.0.1','--port','5174','--strictPort','--config',"$root/frontend/vite.config.ts") 5174 'vite/bin/vite.js'}
Write-Output 'Owned acceptance processes launched; readiness will be checked separately.'
