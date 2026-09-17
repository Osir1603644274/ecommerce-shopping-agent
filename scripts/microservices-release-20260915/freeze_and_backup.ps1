$ErrorActionPreference='Stop'
$root='F:/agent'
$out='D:/agent-experiments/microservices-release-20260915-attempt001'
$maintenance="$root/.runtime/merged-commerce/microservices-maintenance.json"
if(Test-Path $maintenance){throw 'Existing maintenance must be inspected before another attempt'}
$old=(docker inspect commerce-full-live-java-20260915|ConvertFrom-Json)[0]
if($old.Config.Labels.'commerce.release' -ne '20260915-a1'){throw 'Unexpected live container identity'}
@{attempt='20260915-a1';phase='FROZEN_BACKUP';startedAtUtc=[datetime]::UtcNow.ToString('o');evidenceDirectory=$out}|ConvertTo-Json|Set-Content $maintenance -Encoding utf8
& "$root/scripts/merged-commerce.ps1" -Action freeze-agent
if($LASTEXITCODE -ne 0){throw 'Agent freeze failed'}
docker stop -t 30 commerce-full-live-java-20260915
if($LASTEXITCODE -ne 0){throw 'Java freeze failed'}
& "$root/.venv/Scripts/python.exe" -X utf8 "$root/scripts/microservices-release-20260915/live_backup.py" --frozen 2>&1 | Tee-Object -FilePath "$out/frozen-live-backup.log"
if($LASTEXITCODE -ne 0){throw 'Fresh backup failed; keep maintenance and artifacts for inspection, do not start old writers blindly'}
Write-Output 'Frozen backup verified. Services remain frozen for guarded schema migration.'
