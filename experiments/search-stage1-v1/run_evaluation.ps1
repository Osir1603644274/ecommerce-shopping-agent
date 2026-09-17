$ErrorActionPreference = 'Stop'
$taskPython = 'F:/agent/.venv/Scripts/python.exe'
$taskRoot = 'D:/agent-datasets/search-stage1-v1'
$taskCode = $PSScriptRoot
$taskEpoch = (Get-Content -Raw -Encoding utf8 "$taskRoot/evaluation/selected-checkpoint.json" | ConvertFrom-Json).epoch
foreach ($taskSource in @('kuaisearch','multicpr')) {
    foreach ($taskSplit in @('dev','test')) {
        & $taskPython "$taskCode/evaluate.py" evaluate --source $taskSource --split $taskSplit --epoch $taskEpoch --qrels-dir "$taskRoot/qrels/final-v1/$taskSource-$taskSplit"
        if ($LASTEXITCODE -ne 0) { throw "Evaluation failed: $taskSource $taskSplit" }
    }
}
# Reuses frozen scores and qrels. Does not train, infer, relabel or reselect.
