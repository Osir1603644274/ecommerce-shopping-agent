"""Bind every actual final model stop reason, including nonempty controller successes."""
from collections import Counter
from pathlib import Path
from retrieval_runtime import read_json, sha, write_once
from agent_quality_review import verify_run

ROOT=Path('D:/agent-datasets/search-closure-v1')
RUN=ROOT/'agent-execution-preparation/agent-pairs-v1'

def main():
    verify_run(RUN/'COMPLETE.json','7c24fd27a0d42fe9495aea2e00ba1749f3b971a663590fbc2c8c8442b630f9ad')
    records=[]
    for slot in sorted((RUN/'attempts').glob('slot-*')):
        receipt=read_json(slot/'RECEIPT.json')
        events=sorted((slot/'events').glob('*-model-response.json'))
        assert len(events)==2
        path=events[-1];response=read_json(path)['data']['response'];choice=response['choices'][0]
        content=choice['message'].get('content') or '';usage=response.get('usage') or {}
        records.append({'slot':slot.name,'outcome':receipt['outcome'],'finish_reason':choice['finish_reason'],
            'content_nonempty':bool(content),'content_characters':len(content),'completion_tokens':usage.get('completion_tokens'),
            'reasoning_tokens':(usage.get('completion_tokens_details') or {}).get('reasoning_tokens'),
            'final_response':{'path':str(path),'sha256':sha(path)},'receipt_sha256':sha(slot/'RECEIPT.json')})
    assert len(records)==40 and all(r['finish_reason']=='length' and r['completion_tokens']==512 for r in records)
    assert Counter((r['outcome'],r['content_nonempty']) for r in records)=={('SUCCEEDED',True):39,('FAILED',False):1}
    target=ROOT/'reports/agent-output-budget.md'
    raw='''# Agent 输出预算：全部原始最终响应核对

40 个槽位的最终模型响应均为 `finish_reason=length`，每次实际使用 512 个 completion tokens，全部触及冻结的输出预算。没有一条以模型正常 `stop` 结束。

其中 39 次有非空正文，被当前控制器记为 SUCCEEDED；另 1 次正文为空，记为 FAILED。**39 次执行成功不代表 39 个完整答案。** 512 的使用量包含接口记录的 reasoning；这解释了正文可能很短，不能据此认定网络响应丢失。

保留原预算、原文和全部 40 个槽位，独立审查只判断实际生成内容，不补写句子，不删失败，不改预算重跑。v2 分数字段恢复也没有恢复或续写答案。

这组固定预算实验可描述实际延迟、执行结果与受限输出的回答质量；不能代表充分输出预算下的 Agent 能力，也不能把预算造成的所有问题单独归因于检索器。两方案使用相同预算，不消除该预算对比较的限制。

本文件补充先前 agent-execution.md 对空答案失败的说明。下一轮若研究输出预算，应另行预注册预算/结束原因处理与完整性检查，不能覆盖本轮运行或回流修改已消费搜索测试。
'''
    if target.exists():assert target.read_text(encoding='utf-8')==raw
    else:target.write_text(raw,encoding='utf-8')
    write_once(ROOT/'reports/agent-output-budget.sources.json',{'report_sha256':sha(target),'generator_sha256':sha(Path(__file__)),
        'complete_sha256':sha(RUN/'COMPLETE.json'),'final_response_count':40,'length_finish_count':40,
        'nonempty_controller_successes':39,'empty_failures':1,'records':records,'new_model_calls':0,'production_activation':False})
    print({'final_responses':40,'length_finish':40,'nonempty_controller_successes':39,'report_sha256':sha(target)})

if __name__=='__main__':main()
