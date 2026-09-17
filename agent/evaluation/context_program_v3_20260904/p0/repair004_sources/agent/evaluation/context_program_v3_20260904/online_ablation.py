"""P5 bounded live readback of compiled stress contexts; no answer-quality labels."""
import argparse
import asyncio
from collections import Counter
import json
from openai import AsyncOpenAI
from agent.app.settings import settings
from agent.app.context_compiler_v1 import compile_context_v1, ContextBudgetExceeded
from agent.evaluation.context_program_v1_20260904 import budget_ablation as lib
from .common import HERE, RecordedClient, append, canonical, check_freeze, file_sha, freeze, json_new, now, sha

TOOL = {'type':'function','function':{'name':'record_compiled_readback','strict':True,
    'description':'Copy only fields from the supplied compiled context.',
    'parameters':{'type':'object','additionalProperties':False,'required':['os','budgetMinor','softUseCases','historyCount'],
        'properties':{'os':{'type':'string'},'budgetMinor':{'type':'number'},
            'softUseCases':{'type':'array','items':{'type':'string'}},'historyCount':{'type':'integer'}}}}}
PROMPT = ('Call record_compiled_readback exactly once, with no prose. Copy os from the value of '
    'context.hardConstraints whose key is os. Copy budgetMinor from hardConstraints key price_minor. '
    'softUseCases is exactly the ordered useCase strings from context.softPreferences, or [] if absent. '
    'historyCount is the length of context.historySummaries, or 0 if absent. '
    'Never use obsolete budget claims from history to override hardConstraints. Never reconstruct removed fields.')


def prepare():
    out = HERE / 'p5/online001'; out.mkdir(parents=True, exist_ok=False)
    source = HERE / 'p5/pressure001/inputs.json'
    inputs = [r for r in json.loads(source.read_text(encoding='utf-8'))
              if r['caseId'].endswith(('-p1','-p8'))]
    schedule = []
    for case in inputs:
        run = lib.make_run(case['caseId'],5)
        items = lib.context_items_from_pack(lib.Pack(case['payload']),run)
        for component in ('ALL','NO_SOFT_PREFERENCE','PROTECTED_ONLY'):
            selected = lib.variant(items,component)
            protected = {i.item_id for i in selected if i.item_type in lib.PROTECTED}
            protected_tokens = sum(i.estimated_tokens for i in selected if i.item_id in protected)
            for policy in lib.POLICIES:
                for budget in (4000,1000,100):
                    row = {'caseId':case['caseId'],'component':component,'policy':policy,'budget':budget,
                        'originalPreferenceCount':len(case['payload']['softPreferences']),
                        'originalHistoryCount':len(case['payload']['historySummaries'])}
                    try:
                        compiled = compile_context_v1(run,selected,budget_tokens=budget,
                            tool_schema_hash=sha(TOOL),model_config_hash=sha({'model':settings.deepseek_model,'thinking':'disabled'}),
                            query=case['payload']['goal'],history_policy=policy)
                        visible = compiled.model_view
                        expected = {'os':next(r['value'] for r in visible['hardConstraints'] if r['key']=='os'),
                            'budgetMinor':next(r['value'] for r in visible['hardConstraints'] if r['key']=='price_minor'),
                            'softUseCases':[r['useCase'] for r in visible.get('softPreferences',[])],
                            'historyCount':len(visible.get('historySummaries',[]))}
                        assert protected <= {i.item_id for i in compiled.receipt.selected_items}
                        row.update(status='COMPILED',context=visible,expected=expected,
                            receipt=compiled.receipt.model_dump(by_alias=True,mode='json'))
                    except ContextBudgetExceeded:
                        if protected_tokens <= budget: raise
                        row.update(status='EXPECTED_PROTECTED_OVERFLOW',protectedTokens=protected_tokens)
                    schedule.append(row)
    json_new(out / 'schedule.json',schedule)
    json_new(out / 'protocol.json',{'at':now(),'sourceSha256':file_sha(source),'rows':len(schedule),
        'plannedCalls':sum(r['status']=='COMPILED' for r in schedule),'maxCalls':144,
        'components':['ALL','NO_SOFT_PREFERENCE','PROTECTED_ONLY'],'budgets':[4000,1000,100],
        'primary':'exact typed readback of visible fields; hard constraints unchanged',
        'secondary':'actual provider cost and loss of removable context; no human quality claim',
        'oracle':'derived and frozen from compiled view before calls; discarded preferences expected absent, not hallucinated',
        'noRetry':True,'noDefaultSwitch':True})
    print('P5 online schedule prepared:',len(schedule))


async def execute():
    out = HERE / 'p5/online001'
    if (out/'started.json').exists(): raise RuntimeError('no_overwrite')
    # Only the completed main confirmation can authorize online extensions.
    gate_path = HERE / 'p4/confirm001/result.json'
    gate = json.loads(gate_path.read_text(encoding='utf-8'))
    if gate['status'] != 'PASS_EXECUTION': raise RuntimeError('P4_confirmation_not_passed')
    analysis_path = HERE / 'p4/confirm001/analysis.json'
    analysis = json.loads(analysis_path.read_text(encoding='utf-8'))
    if not analysis['costThresholdMet'] or analysis['formalPopulationNI'] != 'PASS':
        raise RuntimeError('main_experiment_acceptance_required_for_dependent_online_extension')
    schedule = json.loads((out/'schedule.json').read_text(encoding='utf-8'))
    json_new(out/'started.json',{'at':now(),'gateSha256':file_sha(gate_path),'scheduleSha256':file_sha(out/'schedule.json')})
    freeze(out/'source_freeze.json')
    result = []
    async with AsyncOpenAI(api_key=settings.deepseek_api_key,base_url='https://api.deepseek.com/beta',timeout=45,max_retries=0) as provider:
        client = RecordedClient(provider,phase='P5',output=out)
        for i,row in enumerate(schedule):
            record = {k:row[k] for k in ('caseId','component','policy','budget','status','originalHistoryCount','originalPreferenceCount')}
            if row['status']=='COMPILED':
                client.binding = {'attempt':'online001','ordinal':i,**record}
                try:
                    response = await client.create(model=settings.deepseek_model,
                        messages=[{'role':'system','content':PROMPT},{'role':'user','content':canonical({'context':row['context']})}],
                        tools=[TOOL],tool_choice={'type':'function','function':{'name':'record_compiled_readback'}},max_tokens=4096)
                    choice = response.choices[0]
                    calls = choice.message.tool_calls or []
                    if choice.finish_reason=='length' or len(calls)!=1 or calls[0].function.name!=TOOL['function']['name']:
                        raise ValueError('invalid_tool_response')
                    actual = json.loads(calls[0].function.arguments)
                    record.update(actual=actual,expected=row['expected'],exact=actual==row['expected'],
                        usage=response.usage.model_dump() if response.usage else None)
                except Exception as exc:
                    record.update(exact=False,error=type(exc).__name__)
                    if client.halted:
                        append(out/'results.jsonl',record)
                        json_new(out/'stop.json',{'at':now(),'reason':str(exc),'completed':len(result)})
                        break
            result.append(record); append(out/'results.jsonl',record)
            print(f'P5 {i+1}/{len(schedule)} {record.get("exact",record["status"])}',flush=True)
    check_freeze(out/'source_freeze.json')
    live = [r for r in result if r['status']=='COMPILED']
    groups = {}
    for component in ('ALL','NO_SOFT_PREFERENCE','PROTECTED_ONLY'):
        selected = [r for r in live if r['component']==component]
        groups[component]={'calls':len(selected),'exact':sum(r.get('exact',False) for r in selected),
            'totalTokens':sum((r.get('usage') or {}).get('total_tokens',0) for r in selected),
            'softPreferenceRetained':sum(bool((r.get('expected') or {}).get('softUseCases')) for r in selected)}
    json_new(out/'result.json',{'at':now(),'rows':len(result),'plannedRows':len(schedule),
        'liveCalls':len(live),'byComponent':groups,'statusCounts':dict(Counter(r['status'] for r in result)),
        'status':'PASS_BOUNDED_READBACK' if len(result)==len(schedule) and all(r.get('exact') for r in live) else 'HOLD_READBACK',
        'boundary':'synthetic typed readback and actual tokens; not end-to-end answer quality or production budget recommendation'})


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--prepare',action='store_true');p.add_argument('--execute',action='store_true');a=p.parse_args()
    if a.prepare: prepare()
    elif a.execute: asyncio.run(execute())
    else: p.error('choose --prepare or --execute')
