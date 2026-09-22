# Foreground process; Ctrl+C stops only this isolated evaluation BFF.
$env:PYTHONPATH='F:/agent/agent'
$env:BACKEND_BASE_URL='http://127.0.0.1:18080'
$env:REDIS_URL='redis://127.0.0.1:16379/0'
$env:CUSTOMER_SUPPORT_AGENT_ENABLED='true'
$env:COMMERCE_DEMO_ENABLED='true'
$env:COMMERCE_DEMO_PAYMENT_SIMULATION_ENABLED='true'
$env:COMMERCE_WORKSPACE_CART_ENABLED='true'
$env:COMMERCE_WORKSPACE_EPOCH='support-live-001'
$env:CATALOG_WORKSPACE_ENABLED='false'
$env:EVIDENCE_CRITIC_ENABLED='false'
$env:WEB_QUERY_INTAKE_ENABLED='false'
$env:KNOWLEDGE_REVIEW_HYBRID_ENABLED='false'
$env:MEMORY_PROJECTION_CLIENT_ENABLED='false'
$env:PRODUCT_RETRIEVAL_MODE='bm25'
$env:PYTHONUTF8='1'
& F:/agent/.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 18000 --no-access-log
exit $LASTEXITCODE
