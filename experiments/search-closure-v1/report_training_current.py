"""Update only the training report's stage description in a new preserved version."""
from pathlib import Path
from retrieval_runtime import read_json,sha,write_once
ROOT=Path('D:/agent-datasets/search-closure-v1')

def main():
    original=ROOT/'reports/training-run.md';sources=ROOT/'reports/training-run.sources.json'
    bindings=read_json(sources)
    assert sha(original)==bindings['report_sha256']
    for name,digest in bindings['inputs'].items():assert sha(Path(name))==digest,name
    current=[ROOT/'evaluation/final-test-v1/COMPLETE.json',ROOT/'agent-quality-review-v2/frozen/COMPLETE.json',ROOT/'final-selection/SELECTION.json']
    for path in current:read_json(path)
    old='状态：固定三轮训练已完成，训练产物验收通过；最终开发集选参、留出测试与 Agent 实验仍未完成。本报告不声明检索效果提升。'
    new='状态：固定三轮训练、最终开发选参、一次留出测试及公开语料 Agent 配对执行与盲审均已完成。留出证据未支持稳定提升，Agent 启用门未通过，商城真实重排仍缺权威价格条件。此版本仅更新阶段说明；原训练记录及全部数值保留。'
    text=original.read_text(encoding='utf-8');assert text.count(old)==1
    raw=text.replace(old,new)
    target=ROOT/'reports/training-run-current.md'
    if target.exists():assert target.read_text(encoding='utf-8')==raw
    else:target.write_text(raw,encoding='utf-8')
    write_once(ROOT/'reports/training-run-current.sources.json',{'report_sha256':sha(target),'generator_sha256':sha(Path(__file__)),
        'scope':'Stage wording correction only; all original training facts and numerical values retained.',
        'inputs':[{'path':str(p),'sha256':sha(p)} for p in [original,sources,*current]],'old_report_preserved':True,'model_calls':0})
    print({'report':str(target),'sha256':sha(target),'changed_paragraphs':1})

if __name__=='__main__':main()
