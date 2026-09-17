"""Report frozen actual Agent slots, including the failed empty-answer response."""
from pathlib import Path
from collections import Counter
import json
from retrieval_runtime import read_json,sha,write_once
from agent_quality_review import verify_run

ROOT=Path('D:/agent-datasets/search-closure-v1')
RUN=ROOT/'agent-execution-preparation/agent-pairs-v1'
COMPLETE_SHA='7c24fd27a0d42fe9495aea2e00ba1749f3b971a663590fbc2c8c8442b630f9ad'

def main():
    _,_,report=verify_run(RUN/'COMPLETE.json',COMPLETE_SHA)
    receipts=[read_json(p) for p in sorted((RUN/'attempts').glob('*/RECEIPT.json'))]
    responses=[];failures=[]
    for path in sorted((RUN/'attempts').glob('*/events/*-model-response.json')):
        row=read_json(path);data=row['data']
        if data.get('response'):
            response=data['response'];usage=response.get('usage') or {}
            responses.append({'path':str(path),'sha256':sha(path),'response_id':response['id'],
                'response_model':response.get('model'),'prompt_tokens':usage.get('prompt_tokens',0),
                'completion_tokens':usage.get('completion_tokens',0),'total_tokens':usage.get('total_tokens',0)})
    assert len(receipts)==40 and len(responses)==80 and len({x['response_id'] for x in responses})==80
    failure_path=RUN/'attempts/slot-040/RECEIPT.json';failed=read_json(failure_path)
    event_path=RUN/'attempts/slot-040/events/0010-model-response.json';event=read_json(event_path)['data']['response'];choice=event['choices'][0]
    assert failed['outcome']=='FAILED' and choice['finish_reason']=='length' and choice['message']['content']==''
    assert event['usage']['completion_tokens']==event['usage']['completion_tokens_details']['reasoning_tokens']==512
    diagnostic={'slot':40,'outcome':'FAILED','controller_code':'catalog_agent_ValueError','underlying_condition':'empty catalog final answer',
        'finish_reason':choice['finish_reason'],'final_content_empty':True,'completion_tokens':512,'reasoning_tokens':512,
        'logical_slot_reissued':False,'classification':'Real model response exhausted fixed output budget before final answer; not a retrieval failure.',
        'evidence':[{'path':str(p),'sha256':sha(p)} for p in [failure_path,event_path]],'http_attempts':failed['http_attempt_count']}
    write_once(ROOT/'reports/agent-failed-slot-diagnosis.json',diagnostic)
    lines=['# 真实 Agent 配对运行：已完成调用，质量另行盲审','',
        '固定 20 条开发查询，每来源 10 条，每条分别经过冻结旧方案和新方案，共 40 个实际槽位。39 次成功、1 次失败，无补跑。',
        '运行路线为模型选工具 → 现有 call_tool → 全库语料检索 → 模型回答。它不代表网页商城 react_v1 全控制器验收。','',
        '| 方案 | 槽位 | 成功 | 失败 | 全部终态墙钟 P50/s | 全部终态墙钟 P95/s | 实际单查询检索 P95/s |',
        '|---|---:|---:|---:|---:|---:|---:|']
    for arm,v in report['arms'].items():
        lines.append(f'| {arm} | {v["slot_count"]} | {v["succeeded"]} | {v["failed"]} | {v["completed_wall"]["p50_seconds"]:.3f} | {v["completed_wall"]["p95_seconds"]:.3f} | {v["actual_single_query_search"]["p95_seconds"]:.3f} |')
    ratio=report['arms']['winner']['completed_wall']['p95_seconds']/report['arms']['baseline']['completed_wall']['p95_seconds']
    totals={k:sum(x[k] for x in responses) for k in ['prompt_tokens','completion_tokens','total_tokens']}
    lines += ['',f'全部终态 P95 比值为 {ratio:.4f}，满足预定不超过 1.2 的延迟门。这里包含真实模型/检索调用及观察到的加载与缓存状态，排除启动时的资产哈希；只有 20 对固定开发查询，不能外推生产分布或替代质量门。',
        '', '失败位于 slot-040 的旧方案：召回成功、两次模型 HTTP 请求均有响应，但最后响应 finish_reason=length，512 个 completion tokens 全部用于 reasoning，content 为空。控制器拒绝空答案并记 FAILED。失败没有改成成功，也没有更改预算后补跑。',
        '',f'实际记录 80 个不同模型响应。接口报告 prompt tokens={totals["prompt_tokens"]}、completion tokens={totals["completion_tokens"]}、total tokens={totals["total_tokens"]}。这是本次 Agent 接口用量，不是 Codex 标注或整个目标的 Token 用量。',
        '', '调用请求配置为 deepseek-v4-flash；响应中的模型标识单独保存在证据中。所有回答将由独立窗口仅依据自身工具证据审查引用、事实依据、显式要求、不确定性披露与商城边界。1 次失败保留为 NOT_ASSESSABLE，不能通过删除失败条目提高配对质量指标。',
        '', '当前文档只报告实际执行与故障，不宣称回答质量通过、搜索能力提升或生产启用。完整结果目录：'+str(RUN)+'.']
    out=ROOT/'reports/agent-execution.md';raw='\n'.join(lines)+'\n'
    if out.exists():assert out.read_text(encoding='utf-8')==raw
    else:out.write_text(raw,encoding='utf-8')
    write_once(ROOT/'reports/agent-execution.sources.json',{'report_sha256':sha(out),'generator_sha256':sha(Path(__file__)),
        'complete_sha256':COMPLETE_SHA,'run_report_sha256':sha(RUN/'report.json'),'response_bindings':responses,
        'interface_token_totals':totals,'http_attempts':sum(r['http_attempt_count'] for r in receipts),
        'outcomes':dict(Counter(r['outcome'] for r in receipts)),'p95_ratio':ratio,'quality_approval_claimed':False,'production_activation':False})
    print({'status':'ACTUAL_AGENT_EXECUTION_REPORT_WRITTEN','slots':40,'succeeded':39,'failed':1,'http_responses':len(responses),'p95_ratio':ratio,'report_sha256':sha(out)})

if __name__=='__main__':main()
