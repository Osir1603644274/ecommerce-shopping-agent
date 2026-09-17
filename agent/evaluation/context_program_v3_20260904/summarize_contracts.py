"""Extract current P7 contract checks from the real full-suite JUnit receipt."""
from collections import Counter
import json
from xml.etree import ElementTree as ET
from .common import HERE, file_sha, json_new, now

GROUPS={
    'Memory_Context':('test_memory_projection_client','test_memory_context_projection',
        'test_memory_contextpack_integration','test_memory_v3_runtime','test_memory_runtime_bridge','test_memory_governance'),
    'MultiAgent_Context':('test_multi_agent_v2','test_multi_agent_runtime_v2'),
    'EvaluationArmIsolation':('test_evaluation_context_arm',),
}

if __name__=='__main__':
    source=HERE/'p8/full005/pytest.xml';root=ET.parse(source).getroot()
    guard=json.loads((HERE/'p8/full005/http_guard.json').read_text(encoding='utf-8'))
    sourcecheck=json.loads((HERE/'p8/full005/source_verification.json').read_text(encoding='utf-8'))
    assert guard['externalProviderCalls']==0 and sourcecheck['passed']
    details=[]
    for case in root.iter('testcase'):
        module=case.get('classname','').split('.')[1]
        group=next((g for g,names in GROUPS.items() if module in names),None)
        if not group:continue
        status='FAILED' if case.find('failure') is not None or case.find('error') is not None else 'SKIPPED' if case.find('skipped') is not None else 'PASSED'
        details.append({'group':group,'classname':case.get('classname'),'name':case.get('name'),'status':status})
    groups={g:dict(Counter(r['status'] for r in details if r['group']==g)) for g in GROUPS}
    assert all(groups.values()),'missing_contract_group'
    result={'at':now(),'status':'PASS_OFFLINE_CONTRACTS' if all(r['status']=='PASSED' for r in details) else 'HOLD',
        'groups':groups,'cases':len(details),'tests':details,'providerCalls':0,
        'sourceJUnitSha256':file_sha(source),'sourceVerificationSha256':file_sha(HERE/'p8/full005/source_verification.json'),
        'boundary':'actual test execution with fixture transports; not live Java/auth authority, model utility or combined production validation',
        'liveCombination':'NOT_RUN_DEPENDENT_MAIN_GATE','productionDefaultsChanged':False}
    json_new(HERE/'p7/offline_contracts.json',result);print({k:v for k,v in result.items() if k!='tests'})
