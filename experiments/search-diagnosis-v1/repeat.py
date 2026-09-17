import asyncio,json,sys
from pathlib import Path
from run_plans import ROOT,REPO,verify,write
sys.path.insert(0,str(REPO))
async def main():
    verify()
    from agent.app.catalog_conversation import plan_turn,transition,answer_turn
    from agent.app.catalog_model_client import close_client
    from agent.app.settings import settings
    settings.catalog_workspace_reuse_model_client=True
    out=ROOT/'repeats';out.mkdir(exist_ok=False)
    cases=['M06-T4','M05-T3','M05-T4']
    cup=next(q for q in json.loads((ROOT/'single-inputs.json').read_text('utf-8')) if q['query']=='一杯芝士奶酪')
    write(out/'CONTRACT.json',{'selected':cases+[cup['query_id']],'repeatsEach':2,
        'selectionReason':'observed selective undo failure, erased soft-preference qualification, cancelled constraint still mentioned, possible cup qualifier deletion',
        'method':'exact saved pre-turn workspace; answer repeats separately use fixed original post-turn scope and plan; no new retrieval, no state writes, no best-of selection'})
    try:
        for cid in cases+[cup['query_id']]:
            multi=cid.startswith('M')
            original=json.loads((ROOT/('multi-live' if multi else 'single-plans')/(cid+'.json')).read_text('utf-8'))
            workspace=original['before'] if multi else original['workspace']
            message=original['summary']['expected']['message'] if multi else original['input']['query']
            priorReceipt=original['rawRun']['catalogRouteCall'] if multi else original['receipt']
            for n in [1,2]:
                row={'id':cid,'repeat':n,'message':message,'inputContext':'exact_saved_pre_turn','answerContext':'fixed_original_post_turn_not_new_retrieval'}
                try:
                    row['plan'],row['receipt']=await plan_turn(message,workspace)
                    row['samePlanInputSha']=row['receipt']['inputSha256']==priorReceipt['inputSha256']
                    assert row['samePlanInputSha']
                    row['nextQuery']=transition(workspace.get('catalogSearch'),row['plan'])[0]['query']
                    if multi:
                        row['answer'],row['answerReceipt']=await answer_turn(message,original['summary']['plan'],original['after']['catalogSearch'])
                        row['sameAnswerInputSha']=row['answerReceipt']['inputSha256']==original['rawRun']['catalogAnswerCall']['inputSha256']
                        assert row['sameAnswerInputSha']
                except Exception as e:row.update(error=type(e).__name__+': '+str(e),errorReceipt=getattr(e,'receipt',None))
                write(out/(cid+'-'+str(n)+'.json'),row)
                print(json.dumps({k:row.get(k) for k in ['id','repeat','plan','nextQuery','error']},ensure_ascii=False),flush=True)
    finally:await close_client();verify()
if __name__=='__main__':asyncio.run(main())
