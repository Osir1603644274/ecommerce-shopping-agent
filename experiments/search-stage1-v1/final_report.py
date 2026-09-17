"""Assemble final evidence only after every required artifact passes its gate."""
import argparse,json,xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from stage1 import digest,dump,log
from retrieve import rows
from integrity import checked_file,verify_qrels,verify_checkpoint
from evaluate import rankings
from report_validation import validate_report
from collections import defaultdict

ROOT=Path("D:/agent-datasets/search-stage1-v1")
CODE=Path(__file__).resolve().parent
LIMITATIONS=[
    "标签来自不同 Codex 上下文，实际均为 gpt-6-astra；同模型误差相关，只能称银标，不能代替人工金标。",
    "缺少价格、成色、库存、店铺/直播归属等字段，UNKNOWN 保留；池外文档不视为负例。",
    "每源仅 80 test query，按类别分层抽样，并非生产流量加权。短 query 仅精确排重，近重复规则不保证语义家族完全隔离。",
    "MultiCPR 的项目 test 来自原生 dev；它的项目 dev 参与选模，因此不是完全未接触目标域的 zero-shot 选择。",
    "本轮 unique 配额按检索器名截断：2000 个选择标记中 character 1980、dense 20、lexical 0；不代表均衡独有候选覆盖。",
    "冻结池中的 reranker_high_or_uncertain 实际取高分前缀，不确定性拼接没有新增候选；本版不声称独立不确定性采样。",
    "训练输入含原始 attr_value，检索及银标库缺失此字段。grade3 二分类目标是完全相关，grade0/1/2 不可统称无关。",
    "nDCG 上下界仅针对固定池；两个下界之差不是未知真实提升幅度的下界。query bootstrap 不覆盖银标系统偏差。",
    "Recall 分母仅池内已知相关商品，不能声称全库召回率；前后 CE 同一个 Top300，Recall@300 相同是候选集合属性。",
    "完整推理侧文件在事后审计时补封；已有原始权重/模型下载哈希，并通过 2400 对 dev 分数重放，但不冒充最初已具备完整事前 fingerprint。",
    "LoRA 是本地显存/吞吐试跑后的执行调整；本轮未跑全参数微调、未切换生产默认模型。",
]

def generate(review_path=None):
    evidence=[]
    def record(path):
        path=Path(path)
        if not path.is_file():raise ValueError(f"Missing required artifact: {path}")
        evidence.append({"path":str(path),"sha256":digest(path),"bytes":path.stat().st_size})
        return path
    def use(path):
        path=record(path)
        return json.loads(path.read_text(encoding="utf-8-sig"))
    selected=use(ROOT/"evaluation/selected-checkpoint.json")
    epoch=selected["epoch"]
    for item in selected["dev_evidence"]:checked_file(item["path"],item["sha256"])
    use(ROOT/"evaluation/selection-policy.json")
    use(ROOT/"sources.json")
    normalization=use(ROOT/"normalization-report.json")
    use(ROOT/"split-report.json")
    record(ROOT/"queries.selected.jsonl")
    use(ROOT/"labeling/collection.json")
    use(ROOT/"labeling/provenance/judge-models.json")
    training=use(ROOT/"training-report.json")
    audit=use(ROOT/"data-audit.json")
    use(ROOT/"models/manifest.json")
    code=use(ROOT/"provenance/code-manifest.json")
    for item in code["files"]:
        checked_file(item["source"],item["sha256"],item["bytes"])
        checked_file(item["snapshot"],item["sha256"],item["bytes"])
    use(ROOT/"training/run-lora-v1/config.json")
    for n in (1,2,3):
        verify_checkpoint(ROOT/"training/run-lora-v1"/f"epoch-{n}")
        use(ROOT/"training/run-lora-v1"/f"epoch-{n}/complete.json")
        use(ROOT/"training/run-lora-v1"/f"epoch-{n}/inference-state.json")
    replay=use(ROOT/"provenance/inference-replay-audit.json")
    if replay["status"]!="PASS" or any(r["max_abs_error"]>1e-5 for r in replay["records"]):raise ValueError("Replay audit failed")
    cohorts={}
    for source in ("kuaisearch","multicpr"):
        for split in ("dev","test"):
            cohort=f"{source}-{split}"
            pool=use(ROOT/"pools"/f"{cohort}.json")
            use(Path(pool["run_dir"])/"state.json")
            use(Path(pool["run_dir"])/"manifest.json")
            use(ROOT/"pools/inputs"/cohort/"inference-receipt.json")
            use(ROOT/"evaluation/scores"/cohort/f"epoch-{epoch}/complete.json")
            use(ROOT/"evaluation/scores"/cohort/f"epoch-{epoch}/inference-receipt.json")
            for suffix in ("","-topup"):
                parent=ROOT/"qrels/base-v1"/(cohort+suffix)
                if suffix and not rows(ROOT/"evaluation/topup"/cohort/"required.jsonl"):continue
                use(parent/"manifest.json")
                record(parent/"author-receipts.jsonl")
            qdir=ROOT/"qrels/final-v1"/cohort
            manifest=verify_qrels(qdir)
            use(qdir/"manifest.json")
            qrels=rows(qdir/"qrels.jsonl")
            expected={q["query_id"] for q in rows(ROOT/"queries.selected.jsonl") if q["source"]==source and q["split"]==split}
            if {q["query_id"] for q in qrels}!=expected or len(expected)!=(20 if split=="dev" else 80):raise ValueError("Final query coverage incomplete")
            result_dir=ROOT/"evaluation/results"/cohort/f"epoch-{epoch}"
            report=use(result_dir/"report.json")
            checked_file(result_dir/"per-query.jsonl",report["per_query_sha256"])
            checked_file(qdir/"qrels.jsonl",report["qrels_sha256"])
            ranks=rankings(ROOT,source,split,epoch)
            per_query=rows(result_dir/"per-query.jsonl")
            labels=defaultdict(dict)
            for row in qrels:labels[row["query_id"]][row["document_id"]]=row["grade"]
            validate_report(report,per_query,ranks,labels)
            for method in ranks:
                values=[r for r in per_query if r["method"]==method]
                if len(values)!=len(expected) or {r["query_id"] for r in values}!=expected or any(r["outside_pool_top10"] for r in values):raise ValueError("Final metric coverage failure")
            agreement=use(ROOT/"labeling/reconciliation"/cohort/"report.json")
            cohorts[cohort]={"queries":len(expected),"pairs":len(qrels),"grades":dict(Counter(str(r["grade"]) for r in qrels)),
                "mean_candidates":len(qrels)/len(expected),"initial_dual_agreement":agreement["agreement"],"initial_disputes":agreement["disputes"],
                "numeric_weighted_kappa":agreement["quadratic_weighted_kappa_numeric_pairs"],"kappa_eligible_pairs":agreement["weighted_kappa_eligible_pairs"],
                "means":report["means"],"paired_before_after":report["paired_before_after"],"qrels_directory":str(qdir)}
    exported=use(ROOT/"export/selected-reranker/export.json")
    for item in exported["files"]:checked_file(ROOT/"export/selected-reranker"/item["name"],item["sha256"],item["bytes"])
    if exported["selected_checkpoint_sha256"]!=digest(ROOT/"evaluation/selected-checkpoint.json") or exported["replay_max_absolute_error"]>1e-5:raise ValueError("Export replay/binding failed")
    latency=use(ROOT/"evaluation/latency/report.json")
    checked_file(ROOT/"evaluation/latency/observations.jsonl",latency["observations_sha256"])
    if latency["selected_checkpoint_sha256"]!=digest(ROOT/"evaluation/selected-checkpoint.json"):raise ValueError("Latency selection drift")
    tests=ROOT/"provenance/tests.xml"
    suites=list(ET.parse(tests).getroot().iter("testsuite"))
    if not suites or any(int(s.attrib.get(k,0)) for s in suites for k in ("failures","errors","skipped")):raise ValueError("Relevant tests not all passing")
    evidence.append({"path":str(tests),"sha256":digest(tests),"bytes":tests.stat().st_size})
    test_receipt=use(ROOT/"provenance/test-receipt.json")
    checked_file(tests,test_receipt["xml_sha256"])
    checked_file(ROOT/"provenance/code-manifest.json",test_receipt["code_manifest_sha256"])
    required_tests=test_receipt["required_tests"]
    observed=sorted(f"{case.attrib.get('classname','')}::{case.attrib['name']}" for case in ET.parse(tests).getroot().iter("testcase"))
    if observed!=required_tests or len(observed)<34:raise ValueError("Required test coverage changed")
    evidence.sort(key=lambda r:r["path"])
    evidence_path=ROOT/"evaluation/final-evidence-manifest.json"
    dump(evidence_path,{"evidence":evidence,"scope":"Complete execution inputs and code/tests; excludes report and reviewer outputs to avoid circular hashes"})
    review=None
    if review_path:
        review=use(review_path)
        if review.get("final_acceptance")!="ACCEPT_WITH_LIMITATIONS":raise ValueError("Independent final review has not accepted bounded execution")
        checked_file(evidence_path,review["final_evidence_manifest_sha256"])
        expected_reviewer=json.loads((ROOT/"provenance/independent-audit-task.json").read_text(encoding="utf-8-sig"))
        if review.get("source_thread_id")!=expected_reviewer["threadId"]:raise ValueError("Independent review author binding mismatch")
    result={"status":"COMPLETE" if review else "EXECUTION_COMPLETE_REVIEW_PENDING","scope":"Ordinary product search stage 1; offline pooled silver evaluation",
        "selected_epoch":epoch,"documents":normalization["documents"],"queries":200,"training_pairs":training["kuaisearch"]["exported"],
        "qrel_pairs":sum(c["pairs"] for c in cohorts.values()),"cohorts":cohorts,"latency":latency["summary"],"tests_passed":sum(int(s.attrib["tests"]) for s in suites),
        "cloud_compute_spend_cny":0,"codex_usage_cost":"Not estimated; zero cloud compute spend does not mean zero Codex account usage",
        "production_switch_authorized":False,"limitations":LIMITATIONS,"evidence":evidence,"independent_review":str(review_path) if review_path else None}
    dump(ROOT/"evaluation/final-report.json",result)
    lines=["# 普通商品搜索第一阶段结果","",f"执行状态：{result['status']}。已完成 200 个真实 query、{result['qrel_pairs']:,} 对银标候选；KuaiSearch 原始人工训练对 41,306，LoRA 3 epochs，开发集选定 epoch {epoch}。", "",
        "下表为固定测试池的 pooled nDCG@10 保守上下界，不能当作完整真值下的点指标。", "",
        "| 测试来源 | Base CE 区间 | 微调 CE 区间 | 下界差及 95% CI | Judged@10 前→后 | 完整池点指标 query |",
        "|---|---:|---:|---:|---:|---:|"]
    def mean(c,m,k):return c["means"][m][k]["mean"]
    def fmt(x):return "NA" if x is None else f"{x:.4f}"
    for source in ("kuaisearch","multicpr"):
        c=cohorts[source+"-test"];delta=c["paired_before_after"]["ndcg_lower_bound_at_10"]
        intervals=[f"[{fmt(mean(c,m,'ndcg_lower_bound_at_10'))}, {fmt(mean(c,m,'ndcg_upper_bound_at_10'))}]" for m in ("base_ce","trained_ce")]
        lines.append(f"| {source} | {intervals[0]} | {intervals[1]} | {fmt(delta['delta'])} [{fmt(delta['ci95'][0])}, {fmt(delta['ci95'][1])}] | {fmt(mean(c,'base_ce','judged_at_10'))} → {fmt(mean(c,'trained_ce','judged_at_10'))} | {c['means']['trained_ce']['ndcg_at_10']['eligible_queries']}/80 |")
    lines += ["", "完整的 BM25、字符、dense、RRF、基线 CE、微调 CE 指标、有效 query 数和池内 Recall 见同目录 final-report.json；逐 query 原始数值见 results/。不由下界差直接宣布真实相关性显著提升。", "", "| 队列 | Query | 候选对 | UNKNOWN | 初始双标一致率 |", "|---|---:|---:|---:|---:|"]
    for name,c in cohorts.items():lines.append(f"| {name} | {c['queries']} | {c['pairs']} | {c['grades'].get('UNKNOWN',0)} | {c['initial_dual_agreement']:.2%} |")
    lines += ["",f"相关测试 {result['tests_passed']} 项通过；2400 对 dev 重放最大误差为 0；导出模型重载误差 {exported['replay_max_absolute_error']}。", "",
        "暖重排部件延迟（20 个 dev 请求，各 3 次；300 候选，含 tokenize/传输/前向；不含模型加载，不是线上端到端）："]
    for method,v in latency["summary"].items():lines.append(f"- {method}：中位数 {v['median_seconds']:.3f}s，p95 {v['p95_seconds']:.3f}s。")
    lines += ["", "边界：",""]+[f"- {s}" for s in LIMITATIONS]
    lines += ["",f"数据及 qrels：`{ROOT}/qrels/final-v1/`。独立复核：`{review_path}`。完整文件凭据见 final-report.json 的 evidence；代码入口：`{CODE}`。"]
    (ROOT/"evaluation/RESULTS.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    log("final_report_written",status=result["status"],qrel_pairs=result["qrel_pairs"])

if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("--review",type=Path);a=p.parse_args();generate(a.review)
