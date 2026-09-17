"""Explicit public-history recommendation mode inside the existing owner-bound workspace."""
import asyncio, copy, json, re, secrets, time
from typing import Literal
from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from . import commerce_workspace as ws
from ..recommendation_catalog import get_recommendation_catalog, fingerprint, SOURCE

class DemoRun(ws.ChatInput):
    case_id: str = Field(alias='caseId',pattern=r'^(demo-\d{2}|cold)$')
    expected_revision: int = Field(alias='expectedRevision',ge=0)
    expected_conversation_id: str = Field(alias='expectedConversationId',pattern=r'^[a-f0-9]{32}$')

class DemoPlan(BaseModel):
    model_config=ConfigDict(extra='forbid')
    action: Literal['recommend','search','like','exclude','undo','compare','cancel','clarify']
    query: str = Field(default='',max_length=200)
    numbers: list[StrictInt] = Field(default_factory=list,max_length=6)
    question: str = Field(default='',max_length=250)

async def plan(message,current):
    exact={'推荐':'recommend','给我推荐':'recommend','根据历史推荐':'recommend','重新推荐':'recommend','撤销':'undo','取消':'cancel'}
    release=message.strip() in {'重新推荐','不限定类型，重新推荐','结束搜索，重新推荐'}
    if current.get('query') and message.strip() in {'推荐','给我推荐','根据历史推荐'}:
        return DemoPlan(action='clarify',question='当前仍在商品搜索范围内，暂未启用范围内个性化重排。如需结束搜索并按历史重新推荐，请输入“重新推荐”。'),{'kind':'scope_guard'}
    if release:return DemoPlan(action='recommend'),{'kind':'explicit_scope_release'}
    if message.strip() in exact:return DemoPlan(action=exact[message.strip()]),{'kind':'explicit_command'}
    from ..catalog_conversation import model_call
    tool={'type':'function','function':{'name':'recommendation_action','description':'解释当前公开数据推荐演示中的用户操作','parameters':DemoPlan.model_json_schema()}}
    reply,receipt=await model_call([
        {'role':'system','content':'你是购物 Agent 中的公开数据推荐工具路由器。只调用 recommendation_action 一次。'
         'recommend 按演示历史推荐，search 在同一英文商品目录搜索；query 仅用于 search，将中文需求翻译为英文检索短语，不能编造商品。'
         'like 把明确喜欢的当前展示编号加入本次演示偏好；exclude 排除明确不喜欢的当前展示编号；undo 撤销上一轮 search/like/exclude。'
         'compare 比较当前列表明确指定的至少两个编号；cancel 结束演示偏好；指代不明或没有支持的条件用 clarify。'
         '只能从当前列表取编号，不能生成 ID 或修改用户身份。不支持价格/库存/下单/功效保证；遇到这些需求说明缺失。'
         '不能把公开历史说成真实登录用户的历史。偏好仅在当前演示会话生效。'},
        {'role':'user','content':json.dumps({'message':message,'display':[{'number':n,'title':i['title']} for n,i in enumerate((current.get('scope') or {}).get('items',[]),1)],'query':current.get('query','')},ensure_ascii=False)}],tools=[tool],max_tokens=500)
    calls=reply.tool_calls or []
    if len(calls)!=1 or calls[0].function.name!='recommendation_action':raise ValueError('invalid_recommendation_plan')
    decision=DemoPlan.model_validate_json(calls[0].function.arguments)
    if current.get('query') and decision.action=='recommend':
        return DemoPlan(action='clarify',question='当前搜索范围仍保留；如需跨目录按历史推荐，请明确输入“重新推荐”。'),receipt
    return decision,receipt

def apply_plan(service,current,decision):
    state=copy.deepcopy(current);scope=state.get('scope') or {};items=scope.get('items',[])
    if scope and (scope.get('modelVersion')!=service.version or scope.get('source')!=SOURCE):raise ValueError('stale_recommendation_model')
    if len(decision.numbers)!=len(set(decision.numbers)) or any(n<1 or n>len(items) for n in decision.numbers):raise ValueError('invalid_display_reference')
    if scope:
        payload={k:v for k,v in scope.items() if k!='scopeId'}
        if fingerprint(payload)!=scope['scopeId']:raise ValueError('changed_recommendation_scope')
    notice=''
    if decision.action=='clarify':return state,decision.question or '请说明想推荐、搜索，或比较当前第几件商品。'
    if decision.action in {'like','exclude','compare'} and not decision.numbers:raise ValueError('missing_display_reference')
    if decision.action=='compare':
        if len(decision.numbers)<2:raise ValueError('compare_requires_two_items')
        selected=[items[n-1] for n in decision.numbers]
        return state,render(selected,'只比较原始标题、品牌和类目；价格、效果与库存未知。')
    if decision.action=='undo':
        if not state.get('undo'):return state,'当前没有可撤销的演示修改。'
        previous=state['undo'].pop()
        for key in ['history','seen','excluded','query','scope']:state[key]=previous[key]
        return state,render((state.get('scope') or {}).get('items',[]),'已撤销上一轮修改，恢复原候选。')
    if decision.action=='cancel':
        state.update(scope=None,excluded=[],query='',undo=[])
        case=service.case(state['caseId']);state.update(history=copy.deepcopy(case['history']),seen=case['seen'][:])
        return state,'已清除本次演示的临时偏好与候选。公开原始历史保持不变。'
    if decision.action in {'like','exclude','search'}:
        state.setdefault('undo',[]).append({k:copy.deepcopy(state.get(k)) for k in ['history','seen','excluded','query','scope']})
        state['undo']=state['undo'][-10:]
    if decision.action=='search':
        if not decision.query.strip():raise ValueError('empty_search_query')
        state['query']=decision.query.strip()
    elif decision.action=='recommend':state['query']=''
    elif decision.action=='like':
        for n in decision.numbers:
            item=items[n-1]['sourceItemId']
            state['history'].append({'item_id':item,'timestamp':int(time.time()),'event_kind':'explicit_demo_like','timestamp_kind':'unix_seconds'})
            if item not in state['seen']:state['seen'].append(item)
        notice=('已记录本次演示偏好，保留当前搜索词；如需跨目录按偏好推荐，请输入“重新推荐”。'
                if state.get('query') else '已把你选中的商品作为本次演示偏好，重新推荐。')
    elif decision.action=='exclude':
        state['excluded']=sorted(set(state['excluded'])|{items[n-1]['sourceItemId'] for n in decision.numbers})
        notice='已排除选中的商品，其他原始历史不变。'
    state['scope']=service.recommend(state['history'],state['seen'],excluded=state['excluded'],query=state.get('query',''))
    if state['scope']['fallback']=='no_supported_positive_history':notice='没有可用的偏好历史，先展示训练时间段的热门商品。'
    return state,render(state['scope']['items'],notice or ('在同一商品目录搜索。' if state.get('query') else '根据公开评价历史推荐；这不是你的真实购物记录。'))

def render(items,notice):
    def cell(x):return str(x or '未知').replace('|','\\|').replace('\n',' ')
    text=notice+'\n\n| 编号 | 商品 | 来源记录 |\n|---|---|---|\n'
    for n,item in enumerate(items,1):text+=f"| {n} | {cell(item['title'])} | Amazon · {cell(item['sourceItemId'])} |\n"
    if not items:text+='\n当前条件下没有候选。'
    return text+'\n\n价格、库存及实物信息未核验；本演示仅支持选品、偏好修改与比较。'

@ws.router.get('/recommendation/cases')
async def cases(request:Request,response:Response):
    await ws._identity(request,response)
    try:service=await asyncio.to_thread(get_recommendation_catalog)
    except (OSError,ValueError):raise HTTPException(503,'recommendation artifacts unavailable')
    return {'source':SOURCE,'modelVersion':service.version,'cases':[{'caseId':key,'label':f'公开历史样例 {n}',
             'historyCount':len({h['item_id'] for h in case['history']}),
             'historyTitles':[service.product(i)['title'] for i in dict.fromkeys(h['item_id'] for h in case['history']) if i in service.index][:3]}
             for n,(key,case) in enumerate(service.cases.items(),1)]+[{'caseId':'cold','label':'无历史：冷启动','historyCount':0,'historyTitles':[]}]}

@ws.router.post('/recommendation/run')
async def run(body:DemoRun,request:Request,response:Response):
    from .commerce_controls import snapshot,refresh,save_run,TERMINAL,BOOT
    key,_,_=await ws._identity(request,response)
    async with ws._lock(key):
        state=await ws._load(key);running=await refresh(key)
        if running and running['status'] not in TERMINAL:raise HTTPException(409,'请先结束当前任务')
        if state['conversationId']!=body.expected_conversation_id:raise HTTPException(409,'对话已切换')
        command_digest=fingerprint(body.model_dump())
        receipt=state.get('recommendationReceipt') or {}
        if receipt.get('requestId')==body.request_id:
            if receipt['commandSha256']!=command_digest:raise HTTPException(409,'请求标识已被其他内容使用')
            return await snapshot(key)
        if any(m.get('requestId')==body.request_id for m in state['messages']):raise HTTPException(409,'该请求已处理')
        if state.get('checkout',{} ) and state['checkout'].get('pending'):raise HTTPException(409,'请先处理未决交易')
        current=state.get('recommendationDemo') or {};revision=current.get('revision',0)
        if revision!=body.expected_revision:raise HTTPException(409,'推荐需求版本已变化，请刷新')
        began=time.perf_counter()
        try:
            service=await asyncio.to_thread(get_recommendation_catalog)
            case=service.case(body.case_id)
            if current.get('caseId')!=body.case_id:
                current={'caseId':body.case_id,'revision':revision,'history':copy.deepcopy(case['history']),
                         'seen':case['seen'][:],'excluded':[],'query':'','scope':None,'undo':[]}
            decision,model_receipt=await asyncio.wait_for(plan(body.message,current),timeout=8)
            next_state,text=await asyncio.to_thread(apply_plan,service,current,decision)
        except asyncio.TimeoutError:raise HTTPException(504,'需求理解超时；原推荐状态未修改，请重试')
        except (OSError,ValueError) as exc:raise HTTPException(422,str(exc))
        next_state['revision']=revision+1
        trace=[{'label':'公开历史与候选身份核验','outcome':'completed','detail':{'caseId':body.case_id,'source':SOURCE,'modelVersion':service.version}},
               {'label':'理解推荐操作','outcome':'completed','detail':{'action':decision.action,'receipt':model_receipt}},
               {'label':'执行推荐工具','outcome':'completed','detail':{'scopeId':(next_state.get('scope') or {}).get('scopeId'),'commerceAuthority':False}}]
        state.update(recommendationDemo=next_state,cards=[],selection=None,reference=None,checkout=None,
                     engine='web-'+secrets.token_urlsafe(24))
        state.pop('catalogSearch',None);state.pop('catalogPendingRequest',None);state.pop('productFollowup',None)
        state['messages'].extend([{'role':'user','requestId':body.request_id,'content':body.message,'source':'public_recommendation_demo'},
                                 {'role':'assistant','requestId':body.request_id,'content':text,'cards':[],'flow':trace,'source':'public_recommendation_demo'}])
        state['messages']=state['messages'][-60:]
        state['recommendationReceipt']={'requestId':body.request_id,'commandSha256':command_digest}
        await ws._save(key,state)
        await save_run(key,{'id':secrets.token_hex(16),'requestId':body.request_id,'revision':1,'mode':'continuous','status':'completed',
            'nodes':trace,'engine':state['engine'],'boot':BOOT,'notice':'公开历史推荐演示已完成','nextStage':'done',
            'workflow':'recommendation_demo_v1','durationMs':(time.perf_counter()-began)*1000})
        return await snapshot(key)
