"""Package explicit non-secret artifacts; never enumerate runtime credentials."""
import hashlib
import json
import zipfile
from pathlib import Path

REPO=Path('F:/agent');DOC=REPO/'docs/experiments/recommendation-completion-20260916'
DATA=Path('D:/agent-datasets/recommendation-completion-v1')

def main():
    assert json.loads((DATA/'SEARCH_REGRESSION_RESULT.json').read_text())['status'].endswith('PASSED')
    assert not (DATA/'search-regression-session.private.json').exists()
    addendum='''

## 最后回归修复

普通搜索真实入口“巧克力面包”已完成，耗时13.69秒。首次发现检索成功后，Java交易资料服务不可用导致发布中断；已修复为仅在502/503/504时展示经过来源校验的记录，不提供价格、库存、购买卡片。401/403、版本冲突和来源校验失败仍拒绝，不绕过交易权限。4项针对性单元测试通过；推荐模型、冻结候选和最终评测未变。

Java8080当前不可用是已有运行环境状态；本轮没有启动整个交易服务栈。搜索与推荐可读演示不依赖它，真实交易恢复需另按原部署手册启动并验收。
'''
    report=DOC/'RESULT.md'
    if '## 最后回归修复' not in report.read_text(encoding='utf-8'):
        with report.open('a',encoding='utf-8') as f:f.write(addendum)
    status={'status':'RECOMMENDATION_INTERNSHIP_PROJECT_DELIVERABLE_COMPLETE',
        'completed':['clean real review data','temporal candidate features','LambdaMART training and dev selection',
                     'three-seed neural and causal sequence controls','frozen final evaluation and paired bootstrap',
                     'same-app source-bound demo','real HTTP/browser acceptance','independent metric/hash audit',
                     'resume HTML/PDF and interview/reproduction notes','original search read-only outage regression'],
        'not_claimed':['CTR/CVR','online A/B','cross-source item identity transfer','production readiness','hiring guarantee'],
        'next_user_action':'体验推荐实验室并按INTERVIEW.md复习，使用新版简历投递。',
        'no_additional_training_scheduled':True}
    (DOC/'STATUS.json').write_text(json.dumps(status,ensure_ascii=False,indent=2),encoding='utf-8')
    # Backup original optional-card module before this final availability change.
    module=REPO/'agent/app/catalog_commerce.py';backup=DOC/'integration-before/catalog_commerce.py'
    if not backup.exists():
        current=module.read_text(encoding='utf-8')
        backup.write_text(current[:current.index('\n\nasync def resolve_optional_cards')]+current[current.index('\n\nasync def resolve_cards'):],encoding='utf-8')
    files={}
    def add(path,label):
        assert path.is_file() and '.private.' not in path.name and path.suffix not in {'.env','.log'}
        files[label]=path
    for path in (REPO/'experiments/recommendation-completion-v1').glob('*'):
        if path.suffix in {'.py','.cjs'}:add(path,'experiment-code/'+path.name)
    for path in DOC.rglob('*'):
        if path.is_file() and path.suffix in {'.md','.html','.json','.py','.tsx','.ts'} and path.name!='DELIVERY_MANIFEST.json':
            add(path,'documentation/'+path.relative_to(DOC).as_posix())
    for relative in ['agent/app/recommendation_catalog.py','agent/app/api/recommendation_workspace.py',
                     'agent/app/catalog_commerce.py','agent/app/api/catalog_workspace.py','agent/app/main.py',
                     'agent/app/api/commerce_workspace.py','frontend/src/RecommendationMode.tsx',
                     'frontend/src/Shop.tsx','frontend/src/lib/workspace.ts','frontend/src/ChatShell.css']:
        add(REPO/relative,'application/'+relative)
    for relative in ['final-001/RESULT.json','final-001/SELECTION.json','final-001/MANIFEST.json',
                     'ranking-dev-001/model_0.txt','serving-v1/BUNDLE.json','serving-v1/trees.json',
                     'SERVING_VERIFIED.json','http-acceptance-003/RESULT.json','browser-acceptance-001/RESULT.json',
                     'SEARCH_REGRESSION_RESULT.json','minilm/DOWNLOAD.json']:
        path=DATA/relative
        if path.exists():add(path,'results/'+relative)
    manifest={label:{'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'bytes':p.stat().st_size,
                     'local_path':str(p)} for label,p in sorted(files.items())}
    output=DOC/'DELIVERY_MANIFEST.json';output.write_text(json.dumps(manifest,indent=2,ensure_ascii=False),encoding='utf-8')
    archive=DOC/'推荐算法_复习与复现材料.zip'
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as z:
        for label,path in sorted(files.items()):z.write(path,label)
        z.write(output,'DELIVERY_MANIFEST.json')
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        for label,record in manifest.items():assert hashlib.sha256(z.read(label)).hexdigest()==record['sha256']
    print(json.dumps({'files':len(files),'archive_bytes':archive.stat().st_size,'status':'PACKAGE_VERIFIED'}))

if __name__=='__main__':main()
