"""Build a bounded delivery index; never equate unsuccessful gates with completion."""
from pathlib import Path
from retrieval_runtime import read_json, sha, write_once
ROOT=Path('D:/agent-datasets/search-closure-v1')

def main():
    reports=['final-search-data-card','revision-examples','training-run','final-development','final-test','agent-execution','agent-output-budget','agent-quality-v2','execution-and-replay']
    bindings=[]
    for name in reports:
        report=ROOT/'reports'/f'{name}.md';source=ROOT/'reports'/f'{name}.sources.json'
        if name=='execution-and-replay':
            bindings.append({'path':str(report),'sha256':sha(report),'scope':'Procedure document, not an execution receipt'})
        else:
            receipt=read_json(source)
            assert receipt['report_sha256']==sha(report),name
            bindings.extend([{'path':str(p),'sha256':sha(p)} for p in (report,source)])
    testdir=ROOT/'evaluation/final-test-v1';test=read_json(testdir/'report.json')
    assert sha(testdir/'report.json')==read_json(testdir/'COMPLETE.json')['report_sha256']
    qualitydir=ROOT/'agent-quality-review-v2/frozen';quality=read_json(qualitydir/'report.json')
    assert sha(qualitydir/'report.json')==read_json(qualitydir/'COMPLETE.json')['report_sha256']
    commerce=read_json(ROOT/'commerce-component-v1/DIAGNOSIS.json')
    comparison=test['common_conditional']['main']['comparisons']['winner']
    status={'status':'SEARCH_EXPERIMENT_DELIVERED_FULL_GOAL_PARTIAL',
        'search_experiment_completed':True,'positive_search_improvement_supported':comparison['positive_improvement_supported'],
        'agent_review_completed':True,'agent_gate_passed':quality['agent_gate_passed'],
        'commerce_actual_positive_path_verified':False,'full_goal_complete':False,'production_activated':False,
        'remaining':['Authoritative verified commerce price prerequisite','Actual commerce CE positive/fallback/compare paths','Independent final audit findings resolution'],
        'reports':bindings}
    lines=['# 普通搜索算法闭环：交付入口','',
        '**搜索数据、固定训练、开发选择和一次留出测试已完成；整体目标仍有商城实测缺口。保留原默认策略。**','',
        '| 部分 | 实际完成 | 结论 |','|---|---|---|',
        '| 数据 | 开发40条/3936对；训练200条/8000对；独立项目测试80条/6400对 | 独立上下文模型银标，UNKNOWN 保留，不称人工金标 |',
        '| 搜索与训练 | 763万文档全库索引核验；固定35配置；182有效query、5823有序对、3epoch | 冻结选择 w112/new_epoch2，对照 w211/epoch3 |',
        '| 独立测试 | 80条全部保留、每条80候选、一次真实检索 | 新减旧差值区间及95%区间均跨零，未证明稳定提升 |',
        '| Agent 实际配对 | 20对/40槽位；39非空正文、1空答案失败；80个实际模型响应 | 全部最终响应触及512输出预算，控制器成功不代表完整答案；延迟门通过 |',
        f'| Agent 独立质量复核 | v2 原始分数恢复后重新独立审查 | 启用门={quality["agent_gate_passed"]}；配对质量状态={quality["usefulness"]["status"]} |',
        '| 商城重排 | 实际接口返回25候选，均无已核验价格；实际CE调用0次 | 正向重排、实际回退和重排后比较未完成 |',
        '| 代码回归与历史保护 | 256项不同的已记录针对性测试通过；916历史文件字节一致 | 回归含模拟依赖，不等于完整网页Agent实测 |','',
        '## 阅读入口','']
    names=['最终数据卡','真实清理前后例子','训练记录','35配置与消融','独立测试与逐query指标','实际Agent调用与失败原因','全部40条响应的输出预算限制','最终Agent回答盲审','执行顺序与复算入口']
    for title,name in zip(names,reports):lines.append(f'- [{title}]({(ROOT/"reports"/(name+".md")).as_posix()})')
    lines += ['', '## 保留的失败与修复','',
        '首次CPU评测因全80条均为main、空diagnostic集合报错。保留失败版本，仅修复集合编排；原指标函数、冻结标签和搜索结果不变，6项边界测试通过，未重复模型检索。',
        '第一版Agent盲审输入漏掉了原始score；v2仅从实际模型请求补回400个原始字段。原回答、模型请求、运行收据及v1审查均保留，v1不作最终结论。11项投影及原质量校验测试通过。',
        '', '## 剩余任务','',
        '先为商城实际候选获得可信的价格记录，并确认货币和单位，再使用新实验目录验证CE正向排序、失败回退和后续比较。不得为通过实验补造价格或降低现有商品资格要求。当前读取的439条商品响应全部价格未核验；这是一次接口返回，不是全商品库的总量断言。',
        '独立总审需对照最初11条合同核验现有证据，并处理真实发现。搜索留出已消费，不能据此继续选参、改标或换测试query。TaskState SFT/RL尚未执行，不属于本轮搜索闭环。',
        '', f'商城缺口原始证据：[DIAGNOSIS.json]({(ROOT/"commerce-component-v1/DIAGNOSIS.json").as_posix()})。',
        f'可恢复进度：[STATUS.json]({(ROOT/"STATUS.json").as_posix()})。']
    target=ROOT/'reports/delivery-overview.md';raw='\n'.join(lines)+'\n'
    if target.exists():assert target.read_text(encoding='utf-8')==raw
    else:target.write_text(raw,encoding='utf-8')
    status['overview_sha256']=sha(target);status['generator_sha256']=sha(Path(__file__))
    write_once(ROOT/'reports/delivery-overview.sources.json',status)
    print({'status':status['status'],'report':str(target),'full_goal_complete':False})

if __name__=='__main__':main()
