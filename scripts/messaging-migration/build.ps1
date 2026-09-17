param([switch]$SkipTests)
$ErrorActionPreference='Stop'
$taskRoot=(Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$logRoot=Join-Path $taskRoot 'docs/messaging-migration/evidence'
New-Item -ItemType Directory -Force $logRoot | Out-Null
$log=Join-Path $logRoot ('build-'+(Get-Date -Format 'yyyyMMdd-HHmmss')+'.log')
$mavenArguments=@('-B','-ntp','-s','settings.xml','package','dependency:copy-dependencies','-DoutputDirectory=target/lab-deps')
if($SkipTests) { $mavenArguments=@('-DskipTests')+$mavenArguments }
$container='mq-migration-build-'+(Get-Date -Format 'yyyyMMddHHmmss')
docker run -d --name $container -w /workspace maven:3.9-eclipse-temurin-17 sleep infinity | Out-Null
if($LASTEXITCODE -ne 0) { throw 'Cannot create isolated build container' }
try {
    docker cp "${taskRoot}/backend/src" "${container}:/workspace/"
    docker cp "${taskRoot}/backend/pom.xml" "${container}:/workspace/"
    docker cp "${taskRoot}/backend/settings.xml" "${container}:/workspace/"
    docker exec $container mkdir -p /root/.m2
    if(Test-Path "${env:USERPROFILE}/.m2/repository") { docker cp "${env:USERPROFILE}/.m2/repository" "${container}:/root/.m2/" }
    docker exec $container mvn @mavenArguments *> $log
    $buildExit=$LASTEXITCODE
    docker cp "${container}:/workspace/target" "${taskRoot}/backend/"
    if($buildExit -ne 0) { Get-Content -LiteralPath $log -Tail 35; throw "Build failed; evidence: $log" }
    docker build -f "${taskRoot}/backend/Dockerfile.messaging-lab" -t local-life-messaging-lab:2103 "${taskRoot}/backend" *>> $log
    if($LASTEXITCODE -ne 0) { throw "Lab image build failed; evidence: $log" }
} finally {
    docker stop -t 1 $container | Out-Null
}
Write-Output "Build succeeded; evidence: $log"
