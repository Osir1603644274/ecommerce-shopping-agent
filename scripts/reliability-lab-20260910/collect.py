"""Generate immutable evidence package and readable results from actual measurements."""
import hashlib,json,zipfile,xml.etree.ElementTree as ET
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
LAB=ROOT/'.runtime/reliability-lab-20260910'
DEST=ROOT/'docs/experiments/reliability-20260910'
DEST.mkdir(parents=True,exist_ok=True)
tests=[]
for p in (ROOT/'backend/target/surefire-reports').glob('TEST-*.xml'):
    node=ET.parse(p).getroot()
    if p.stat().st_mtime >= datetime(2026,9,10,15,13,tzinfo=timezone.utc).timestamp():
        tests.append({'suite':node.get('name'),**{k:int(node.get(k,0)) for k in ('tests','failures','errors','skipped')}})
(LAB/'java-test-summary.json').write_text(json.dumps(tests,indent=2),encoding='utf-8')
files=[p for p in LAB.rglob('*') if p.is_file() and p.suffix in {'.py','.java','.yaml','.json','.log','.txt','.xml'} and '__pycache__' not in p.parts]
archive=DEST/'evidence.zip';assert not archive.exists()
manifest={}
with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED) as z:
    for p in files:
        n=p.relative_to(LAB).as_posix();b=p.read_bytes();z.writestr(n,b);manifest[n]=hashlib.sha256(b).hexdigest()
    for p in Path(__file__).parent.glob('*.py'):
        n='runner-source/'+p.name;b=p.read_bytes();z.writestr(n,b);manifest[n]=hashlib.sha256(b).hexdigest()
    for p in (ROOT/'backend/target/surefire-reports').glob('TEST-*.xml'):
        if p.stat().st_mtime>=datetime(2026,9,10,15,13,tzinfo=timezone.utc).timestamp():
            n='java-tests/'+p.name;b=p.read_bytes();z.writestr(n,b);manifest[n]=hashlib.sha256(b).hexdigest()
with zipfile.ZipFile(archive) as z:assert all(hashlib.sha256(z.read(n)).hexdigest()==h for n,h in manifest.items())
(DEST/'manifest.json').write_text(json.dumps({'archiveSha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'members':manifest},indent=2),encoding='utf-8')
cache=json.loads((LAB/'cache-results.json').read_text())
lines=['# 缓存与分布式故障实验结果（2026-09-10）','','**决策状态：仅实验，不修改缓存层数、不修复新发现；等待用户决定。**','','完整交付、冷 JVM 结果、逐轮差异和失败续跑说明见 [DECISIONS.md](DECISIONS.md)。原始 INCOMPLETE 保留，不表示续跑仍未完成。','','## 缓存对照','','|缓存|场景|并发|样本|错误|P50 ms|P95 ms|P99 ms|','|---|---|---:|---:|---:|---:|---:|---:|']
for g in cache['groups']:
    lines.append(f"|{g['tier']}|{g['state']}|{g['concurrency']}|{g['samples']}|{g['errors']}|{g['p50']:.2f}|{g['p95']:.2f}|{g['p99']:.2f}|")
lines+=['','运行状态：'+cache['status']+'。','',
 'l1=仅 Caffeine 实验适配；l2=仅 Redis 实验适配；two=原部署两级缓存。两实例交替访问，256 件合成商品，热点集合 4 件。这里只比较详情读取，不含报价库存与 Agent。',
 'L1/L2 是实验适配器，不是已经验收的新生产实现；L2 固定 TTL，原实现随机抖动；L1 使用进程内重建锁。三轮交错，同机结果，仅描述本配置。未覆盖超过 10000 件的容量淘汰。','',
 '## 故障记录','','|场景|结果|证据|','|---|---|---|']
for name in ('fault-results.json','inbox-results.json','broker-results.json','broker-continuation-results.json'):
    if (LAB/name).exists():
        for row in json.loads((LAB/name).read_text()):
            details={k:v for k,v in row.items() if k not in ('name','status','rounds')}
            lines.append('|'+row['name']+'|'+row['status']+'|'+json.dumps(details,ensure_ascii=False).replace('|','/')+'|')
lines+=['','## 证据限制与保留失败','',
 'attempt001 是测试 Controller 编译缺少 -parameters 引起的预热 500，零有效性能样本；已保留，不归因于缓存。初始实验 Redis 300ms 握手超时导致启动失败，实验参数改为连接 5s、命令 1s；未修改正式 Redis 配置。',
 '消息合同注入、真实 JVM 退出、真实 Kafka 故障分别标注；审计 handler 的去重不能等价于发货副作用恰好一次。整条缓存失效丢失是显式注入的条件，不表示所有正常更新都会丢通知。',
 '故障中的 SELECT SLEEP 用于占用真实共享 Hikari 池，说明资源竞争，不是实际搜索服务故障的完整复现。',
 '原始样本、脚本、Compose 配置与故障结果见 evidence.zip；manifest.json 校验每个成员。未运行模型或真实支付。','',
 '## Java 定向回归','',f"{sum(x['tests'] for x in tests)} tests；failures={sum(x['failures'] for x in tests)}；errors={sum(x['errors'] for x in tests)}；skipped={sum(x['skipped'] for x in tests)}。仅所列定向测试，不能称全仓测试通过。"]
(DEST/'RESULTS.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
print(DEST)
