"""Owner-facing catalog checkpoints: selected business fields, never prompts."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import hashlib
import ast
from pathlib import Path

from .web_query_intake import redact_explicit_secrets

ACTIONS={'search':'首次搜索','refine':'修改需求','new':'切换需求','undo':'撤销上一轮修改',
         'compare':'比较当前候选','cancel':'取消需求','clarify':'澄清需求','inspect':'商品追问'}
MODES={'require':'必须','exclude':'排除','prefer':'优先','avoid':'尽量避开'}


def clean(value, depth=0):
    if depth>8:return '内容已截断'
    if isinstance(value,str):return redact_explicit_secrets(value[:3000])[0]
    if isinstance(value,dict):return {k:clean(v,depth+1) for k,v in value.items()}
    if isinstance(value,list):return [clean(v,depth+1) for v in value[:24]]
    return value if value is None or isinstance(value,(bool,int,float)) else str(value)


def requirements(rows):
    return [{'属性':r.get('facet'),'要求':r.get('value'),'强度':MODES.get(r.get('mode'),r.get('mode')),
             '匹配词':r.get('terms',[])} for r in rows]


def candidates(scope):
    return [{'展示编号':g['number'],'标题':g['title'],
        '来源记录':[{'来源':m['source'],'编号':m['docid']} for m in g['members']]}
        for g in (scope or {}).get('groups',[])]


def code_reference(phase):
    relative,name={'prepare':('catalog_conversation.py','transition'),
       'retrieve':('catalog_service.py','search'),'answer':('catalog_conversation.py','answer_turn'),
       'publish':('api/catalog_workspace.py','work')}[phase]
    path=Path(__file__).parent/relative;raw=path.read_bytes();text=raw.decode('utf-8')
    node=next(n for n in ast.walk(ast.parse(text)) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==name)
    # Only references; never copy routing prompts or provider configuration.
    return dict(file='agent/app/'+relative,function=name,line=node.lineno,
        sha256=hashlib.sha256(raw).hexdigest(),scope='步骤对应实现；不是逐行覆盖率，也不展示模型内部思考。')


def phase_fields(run,phase,seconds):
    plan=run['catalogPlan'];state=run.get('catalogNext') or {};scope=state.get('scope') or {}
    action=plan['action'];searching=action in {'search','refine','new'}
    detail={'workflow':'catalog_workspace_v1','phase':phase,'action':action,
            'scopeId':scope.get('scopeId'),'executionMode':'bounded_catalog_react' if run.get('catalogReact') else 'fixed_catalog_workflow'}
    if phase=='prepare':
        previous=run.get('catalogBefore') or {}
        before=previous.get('requirements') or [];after=state.get('requirements') or []
        inputs={'用户原话':run['message'],'更新前需求':previous.get('query',''), '更新前条件':requirements(before)}
        outputs={'本轮操作':ACTIONS.get(action,action),'完整需求':state.get('query',''),
           '实际检索词':state.get('retrievalQuery',''),'当前条件':requirements(after),
           '新增或修改条件':requirements([r for r in after if r not in before]),
           '移除或修改前条件':requirements([r for r in before if r not in after]),
           '旧候选已失效':searching,'比较编号':plan.get('numbers',[]),
           '下一步':'检索两个来源' if searching else '复用当前候选，无新检索' if action in {'undo','compare'} else '直接处理本轮操作'}
        route=run.get('catalogRouteCall') or {}
        detail.update(purpose=('解释需求并保存版本；后续由受限 ReAct 决策选择只读工具或回答。' if run.get('catalogReact') else '大模型解释本轮操作，程序保存需求与候选版本；此处不是模型自由循环。'),
             model=route.get('model'),modelDurationMs=route.get('durationMs'),decision=ACTIONS.get(action,action))
    elif phase=='retrieve':
        inputs={'实际调用':'CatalogService.search' if searching else '无新检索',
             '需求':state.get('query',''),'检索词':state.get('retrievalQuery',''),
             '条件':requirements(state.get('requirements',[]))}
        outputs={'本步执行':'调用两个来源检索' if searching else '复用已保存候选' if scope else '无需候选',
           '来源返回':[{'来源':s['source'],'返回记录数':len(s.get('hits',[])),
              '耗时秒':s.get('seconds'),'工具参数':{'query':state.get('retrievalQuery') or state.get('query'),
              'source':s['source'],'limit':10}} for s in scope.get('sources',[])] if searching else [],
           '明确条件冲突排除数':len(scope.get('excludedGroups',[])), '待主体审核候选':candidates(scope),
           '下一步':'依据原文审核并回答'}
        detail.update(purpose='读取真实检索返回并核验明确冲突；未记录的分数不补造。',retrievalExecuted=searching)
    elif phase=='answer':
        call=run.get('catalogAnswerCall') or {}
        review=call.get('scopeReview') or {}
        inputs={'需求':state.get('query',''),'条件':requirements(state.get('requirements',[])),
                '操作':ACTIONS.get(action,action),'比较编号':plan.get('numbers',[])}
        outputs={'商品与硬条件判断':[{'原展示编号':r['number'],'判断':{'target':'对应商品','other':'其他商品','unknown':'未知'}[r['relation']],
                '标题原文':r['quote'],'说明':r['reason'],
                '明确硬条件冲突':[{'属性':c['facet'],'要求':c['value'],'标题原文':c['quote'],'说明':c['reason']} for c in r.get('conflicts',[])]}
                for r in review.get('reviews',call.get('subjectReviews',[]))],
             '保留候选':candidates(scope),'排除数量':len(scope.get('semanticExcludedGroups',[])),
             '生成的回答':run.get('catalogAnswer'),'下一步':'保存并展示本轮结果'}
        detail.update(modelCalled=bool(call),purpose='模型依据商品原文回答，程序校验并同步候选；不是隐藏思维链。'
            if call else '返回已确定的操作结果；本步没有调用回答模型。')
    else:
        inputs={'待展示候选':candidates(scope),'回答':run.get('catalogAnswer')}
        outputs={'结果':'本轮已保存','候选数':len(scope.get('groups',[])),
                 '下一步':'等待用户下一条消息；本轮不会自动重新规划'}
        if run.get('catalogTradingSummary') is not None:
            outputs['匹配到商城商品数']=run['catalogTradingSummary']['resolved']
            outputs['当前可预览购买数']=run['catalogTradingSummary']['previewEligible']
            outputs['交易说明']='来源与原文校验值匹配后单独核价、查库存；尚未下单。'
        detail.update(purpose='核对需求版本与证据摘要，再保存回答和同一份候选。')
    finished=datetime.now(timezone.utc)
    if run.get('catalogReact') and phase in {'prepare','retrieve'}:
        outputs['下一步']='回到受限 ReAct 决策；不预先假定会回答或再次检索'
    if run.get('catalogReact') and phase=='retrieve':
        inputs['检索词']=(run.get('catalogPendingDecision') or {}).get('query') or inputs.get('检索词')
    return dict(input=clean(inputs),output=clean(outputs),detail=clean(detail),
        source=code_reference('publish' if phase=='retrieve' and not searching else phase),
        startedAt=(finished-timedelta(seconds=seconds)).isoformat(),finishedAt=finished.isoformat())
