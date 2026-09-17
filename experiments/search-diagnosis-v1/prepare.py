"""Freeze diagnostic inputs and expected states before observing model output."""
import hashlib,json
from pathlib import Path

ROOT=Path('D:/agent-datasets/search-agent-diagnosis-20260913-v1')
DEV=Path('D:/agent-datasets/search-stage1-dev-revision-v6/frozen')
REPO=Path(__file__).resolve().parents[2]
ROOT.mkdir(exist_ok=False)
def write(name,value):
    with (ROOT/name).open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2)
sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
qs=[json.loads(l) for l in (DEV/'queries.jsonl').read_text('utf-8').splitlines()]
for q in qs:q['source']='kuaisearch' if q['query_id'].startswith('s1-ku-') else 'multicpr'
write('single-inputs.json',qs)
def turn(message,query,action,notes):return dict(message=message,expectedQuery=query,expectedAction=action,expectedRoute='catalog',requirements=notes)
dialogs=[
 dict(id='M01',base='睡衣纯棉秋季',purpose='追加与整步撤销',turns=[
  turn('找睡衣纯棉秋季','睡衣纯棉秋季','search','睡衣、纯棉、秋季'),
  turn('只要女款，其他条件保留','女款纯棉秋季睡衣','refine','新增女款，保留纯棉秋季'),
  turn('再加长袖要求','女款纯棉秋季长袖睡衣','refine','保留女款、纯棉、秋季，新增长袖'),
  turn('撤销刚才的长袖要求，其他条件保留','女款纯棉秋季睡衣','undo','只去掉长袖；恢复上一轮需求')]),
 dict(id='M02',base='泰迪狗粮',purpose='否定约束及替换',turns=[
  turn('找泰迪狗粮','泰迪狗粮','search','泰迪犬狗粮'),
  turn('不要鸡肉配方的','泰迪狗粮，不要鸡肉配方','refine','鸡肉是排除项，不能改为正向偏好'),
  turn('还要小颗粒的','泰迪小颗粒狗粮，不要鸡肉配方','refine','保留鸡肉排除，新增小颗粒'),
  turn('把不要鸡肉改成不要牛肉，其他保留','泰迪小颗粒狗粮，不要牛肉配方','refine','解除鸡肉限制，排除牛肉，保留小颗粒')]),
 dict(id='M03',base='玫琳凯男士洗面奶',purpose='跨需求清除品牌预算',turns=[
  turn('找玫琳凯男士洗面奶','玫琳凯男士洗面奶','search','品牌、男士、洗面奶'),
  turn('预算100元以内','100元以内的玫琳凯男士洗面奶','refine','保留商品与品牌，预算上限100，未知价格不能声称已满足'),
  turn('换个需求，找柜子拉篮','柜子拉篮','new','清除洗面奶、品牌、男士、100元预算'),
  turn('只要不锈钢的','不锈钢柜子拉篮','refine','仅继承拉篮，新增不锈钢，不恢复旧预算')]),
 dict(id='M04',base='品胜充电宝主板',purpose='配件本体边界',turns=[
  turn('找品胜充电宝主板','品胜充电宝主板','search','品胜充电宝的主板配件'),
  turn('只要主板，不要整个充电宝','品胜充电宝主板，不要整机','refine','品牌保留，主板本体，不要整个充电宝'),
  turn('换个需求，找iPhone 13手机壳，不要手机','iPhone 13手机壳，不要手机本体','new','配件路由catalog；清除品胜充电宝条件'),
  turn('不要透明的，其他要求保留','iPhone 13手机壳，不要透明款','refine','保留机型与手机壳，透明是排除项')]),
 dict(id='M05',base='老年穿的鞋子女',purpose='软偏好与解除限制',turns=[
  turn('找老年穿的鞋子女','老年女士鞋','search','老年、女鞋'),
  turn('不要带鞋带的','老年女士鞋，不要带鞋带','refine','排除鞋带款'),
  turn('最好防滑，防滑只是偏好，不是必须','老年女士鞋，不要带鞋带，优先防滑但不是必须','refine','保留鞋带排除，防滑是软偏好'),
  turn('鞋带要求取消，其他不变','老年女士鞋，优先防滑但不是必须','refine','解除鞋带限制，保留年龄性别及软偏好')]),
 dict(id='M06',base='夹子晾衣夹木头',purpose='同一轮多个属性的选择性撤销',turns=[
  turn('找夹子晾衣夹木头','木头晾衣夹','search','木头、晾衣夹'),
  turn('还要防风的','防风木头晾衣夹','refine','保留木头与用途，新增防风'),
  turn('再加大号和20个装','大号20个装防风木头晾衣夹','refine','一次新增大号和20个装'),
  turn('只撤销20个装，保留大号和防风','大号防风木头晾衣夹','refine','仅移除数量约束，不能整步退回丢失大号')])]
for d in dialogs:
    d['origin']='assistant_constructed_from_existing_query_not_natural_dialogue'
    d['baseQueryId']=next(q['query_id'] for q in qs if q['query']==d['base'])
write('multi-inputs.json',dialogs)
paths=['agent/app/catalog_conversation.py','agent/app/catalog_service.py','agent/app/catalog_model_client.py',
 'agent/app/catalog_fast_retrieval.py','agent/app/catalog_fast_retrieval_v2.py','agent/app/catalog_fast_retrieval_v3.py',
 'agent/app/catalog_worker.py','agent/app/catalog_fast_selection.py','agent/app/api/catalog_workspace.py','agent/app/api/commerce_controls.py']
refs=[DEV/'queries.jsonl',DEV/'qrels.jsonl',Path('D:/agent-datasets/catalog-latency-20260913-v1/selected-v3.json'),
 Path('D:/agent-datasets/catalog-latency-20260913-v1/dev-eval004/RESULTS.json'),
 Path('D:/agent-datasets/catalog-latency-20260913-v1/live-final001/RUN-RESULTS.json')]
write('CONTRACT.json',dict(status='FROZEN_BEFORE_MODEL_CALLS',singleQueries=40,multiDialogues=6,multiTurns=24,
 sourceCode={p:sha(REPO/p) for p in paths},references={str(p):sha(p) for p in refs},
 inputSha256={p:sha(ROOT/p) for p in ['single-inputs.json','multi-inputs.json']},
 judgments=['clear_error','measured_regression','unresolved_evidence','no_observed_error'],
 controls='same approved runtime v3/w211/epoch-2; original single-query intent remains the relevance target even if rewrite drifts',
 multiControl='independent expected complete state, semantic equivalents accepted; no last-utterance-only strawman baseline',
 qrels='fixed original silver qrels only on unchanged original single-query intent; new multi constraints have no reused qrels',
 stopping='complete fixed batch and provenance review; repeat at most six representative suspected failures twice; no failure quota',
 scope='diagnosis only; no production strategy/weights/qrels changes; no test160 tuning; no new corpus download',
 sourceLimit='existing saved interaction evidence used here is automated acceptance; no natural-user dialogue sample imported'))
print('Frozen 40 singles and 6 x 4 constructed turns at '+str(ROOT))
