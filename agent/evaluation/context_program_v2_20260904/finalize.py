"""Close the bounded gate-stopped run; never call it a completed Context A/B."""
import json
from collections import Counter
from xml.etree import ElementTree as ET

from .common import HERE, ROOT, check_freeze, file_sha, json_new, manifest_check, now, rows


def load(path): return json.loads((HERE / path).read_text(encoding="utf-8"))


def junit(path):
    root = ET.parse(HERE / path).getroot()
    suites = list(root.iter("testsuite"))
    return {"path": path, "sha256": file_sha(HERE / path),
        **{key: sum(int(s.attrib.get(key, 0)) for s in suites) for key in ("tests", "errors", "failures", "skipped")},
        "seconds": sum(float(s.attrib.get("time", 0)) for s in suites)}


def main():
    freeze = check_freeze()
    process = load("p8/full001/process_result.json")
    full = junit("p8/full001/pytest-full.xml")
    full_ok = process["exitCode"] == 0 and full["errors"] == full["failures"] == 0
    diagnostic = junit("p8/environment_diagnostic001/pytest.xml")
    diagnostic_process = load("p8/environment_diagnostic001/result.json")
    p2 = load("p2/attempt001/result.json")
    accounting = load("p8/call_accounting.json")
    p5 = load("p5/offline001/result.json")
    p7 = junit("p7/pytest-isolation.xml")
    old = manifest_check(ROOT / "agent/evaluation/context_program_v1_20260904/FINAL_SHA256SUMS_v2.txt")
    repair = manifest_check(ROOT / "agent/evaluation/context_failure_repair_20260904/SHA256SUMS.txt")
    if old["mismatches"] or repair["mismatches"]: raise RuntimeError("old_artifact_drift")
    tests = list(ET.parse(HERE / "p8/full001/pytest-full.xml").getroot().iter("testcase"))
    p6_modules = {"test_graph_v2_checkpoint", "test_graph_v2_interrupt_resume", "test_graph_v2_idempotent_resume",
        "test_graph_v2_process_restart", "test_checkpoint_recovery_inbox_v2", "test_durable_tool_transport_context",
        "test_graph_v2_react_durable_integration", "test_graph_v2_revision_conflict",
        "test_graph_v2_terminal_atomic_redis_integration", "test_ecommerce_endpoint", "test_web_query_intake"}
    p6 = [t for t in tests if any(m in t.attrib.get("classname", "").split(".") for m in p6_modules)]
    p6_evidence = {"source": full["path"], "tests": len(p6),
        "failures": sum(t.find("failure") is not None or t.find("error") is not None for t in p6),
        "skipped": sum(t.find("skipped") is not None for t in p6),
        "originalCollectionErrorPreserved": "p6/pytest-contract.xml",
        "claim": "existing contract/real-Redis fixture coverage; not fresh browser or production cross-process acceptance"}
    json_new(HERE / "p6/full_suite_supplement.json", p6_evidence)
    family_counts = load("p1/dataset_manifest.json")
    phases = {"P0": "PASS_FROZEN_REPAIR_BASELINE", "P1": "PARTIAL_ENGINEERING_FIXTURES_NOT_FULL_COVERAGE",
        "P2": p2["status"], "P3": "NOT_RUN_DEPENDENCY_P2_HOLD", "P4": "NOT_RUN_DEPENDENCY_P2_HOLD",
        "P5": "OFFLINE_MECHANICS_PASS_ONLINE_NOT_RUN",
        "P6": "CONTRACTS_COVERED_BY_FULL_SUITE_LIVE_EXPANSION_NOT_RUN" if p6 and not p6_evidence["failures"] and not p6_evidence["skipped"] else "HOLD_TEST_EVIDENCE",
        "P7": "CONTRACT_ISOLATION_PASS_ONLINE_NOT_RUN",
        "P8": "AUDIT_CLOSED_HOLD_CONTEXT_DEFAULT" if full_ok else "AUDIT_CLOSED_ORIGINAL_SUITE_SETUP_FAILURES_DIAGNOSED_HOLD"}
    for phase in ("p3", "p4"):
        json_new(HERE / phase / "decision.json", {"status": "NOT_RUN", "reason": "P2 extraction gate HOLD",
            "providerCalls": 0, "automaticRetry": False})
    fallacies = [
        ("Simpson", "Mode and family strata reported; pooled equal pass counts conceal different failure types."),
        ("Ecological", "Synthetic variant count is not an independent user count; family counts reported."),
        ("Berkson", "Selection limited to scripted engineering data; no general population inference."),
        ("Collider", "No efficiency claim conditioned on successful-only executions."),
        ("BaseRate", "No screening prevalence inference; all 96 assigned outcomes retained."),
        ("RegressionToMean", "Old failure-selected data not used to claim causal repair improvement."),
        ("Survivorship", "Four rejected executions and all five repairs retained; no failed-case deletion."),
        ("LookElsewhere", "No significance claim; supplementary goal audit separately labeled."),
        ("ForkingPaths", "Frozen primary gate retained; no post-outcome threshold relaxation or new attempt."),
        ("CorrelationCausation", "P3/P4 not run; no Context quality/cost effect claim."),
        ("ReverseCausality", "Not applicable to descriptive protocol-first request records; no causal claim."),
    ]
    json_new(HERE / "p8/statistical_validation.json", {"status": "ANALYZED_DESCRIPTIVE_ONLY", "checks": [
        {"type": name, "assessment": detail} for name, detail in fallacies], "coverage": "11/11",
        "confidenceIntervals": "NOT_ESTIMATED_FOR_MAIN_AB_NOT_RUN", "noninferiority": "NOT_ESTABLISHED",
        "externalModelReproducibility": "N/A_NONDETERMINISTIC_PROVIDER",
        "offlineCompilerReproducibility": "800 pairs of identical in-memory compilation outcomes",
        "confirmationFamilies": family_counts["confirmationFamilies"]})
    final = {"programId": HERE.name, "at": now(), "decision": "HOLD_EXTRACTION_GATE__MAIN_CONTEXT_EXPERIMENTS_NOT_RUN",
        "phases": phases, "plannedMainExperimentsComplete": False, "allP0P8Passed": False,
        "fullSuite": full, "fullSuitePassed": full_ok, "p6Evidence": p6_evidence, "p7Tests": p7,
        "environmentDiagnostic": diagnostic, "environmentDiagnosticProcess": diagnostic_process,
        "singleRunFullSuiteAllPassed": full_ok,
        "providerRequests": accounting["requests"], "providerTokens": accounting["usage"]["total_tokens"],
        "p2ExecutionCounts": p2["summary"], "goalMarkerAcceptedPatches": load("p2/goal_marker_validation.json")["survivesValidation"],
        "productionSourceFilesChanged": False, "productionDefaultsChanged": False,
        "oldArtifactsChecked": old["checked"], "repairArtifactsChecked": repair["checked"],
        "sourceHashesRechecked": len(freeze["sources"]), "accountingChecksPassed": accounting["status"] == "PASS"}
    json_new(HERE / "FINAL_DECISION.json", final)
    json_new(HERE / "program_registry.json", {"programId": HERE.name, "phases": phases,
        "status": "STOPPED_AT_P2_GATE_WITH_INDEPENDENT_OFFLINE_AUDIT", "next": "NEXT_REPAIR.md"})
    failures = [{"classname": t.attrib.get("classname"), "name": t.attrib.get("name"),
        "failure": (t.find("failure").text if t.find("failure") is not None else t.find("error").text)}
        for t in tests if t.find("failure") is not None or t.find("error") is not None]
    skips = [{"classname": t.attrib.get("classname"), "name": t.attrib.get("name"),
        "reason": t.find("skipped").attrib.get("message")} for t in tests if t.find("skipped") is not None]
    json_new(HERE / "p8/test_exceptions.json", {"failures": failures, "skips": skips})
    passed = full["tests"] - full["failures"] - full["errors"] - full["skipped"]
    report = f'''# Context v2 执行报告

## Material Passport

- Origin Skill: academic-research-suite / experiment-agent
- Origin Mode: run + validate
- Origin Date: 2026-09-04
- Verification Status: ANALYZED_HOLD
- Version Label: context_program_v2_20260904

## 结论

`HOLD_EXTRACTION_GATE__MAIN_CONTEXT_EXPERIMENTS_NOT_RUN`。

执行在 P2 门槛处停止推进付费主实验，独立离线检查和最终回归完成。不是 P0—P8 全部通过，也不是主 Context A/B 已完成。

## 阶段状态

| 阶段 | 结果 |
|---|---|
| P0 | 修复 445 项测试证据、7 项修复产物、22 项旧产物哈希匹配；冻结 {len(freeze['sources'])} 个文件 |
| P1 | 新适配器 7 项测试通过；开发16/确认64条合成会话仅2/4个模板家族，广度及一般统计能力不足，属于部分准备 |
| P2 | 96次场景执行；strict 46/48，非strict 46/48；二者均不满足预注册门槛 |
| P3/P4 | 未运行：依赖 P2，通过门槛未满足 |
| P5 | 800组离线组合，各编译两次一致；均能编译，没有触发保护项溢出；没有在线质量/效率结论 |
| P6 | 首次测试收集有导入路径错误，原记录保留；独立全量回归覆盖相关 {len(p6)} 项用例。未做本轮真实浏览器/新跨进程正式验收 |
| P7 | 202项合同/模拟隔离测试通过；没有开启真实 Memory/Multi-Agent 在线组合实验 |
| P8 | 全量测试 {passed} passed / {full['failures']} failed / {full['errors']} errors / {full['skipped']} skipped；账本与证据核验完成 |

## 失败事实

1. 两次非strict执行把“续航体验”编造成 `battery_hours >= 8`，用户没有给8小时，业务模型也不支持该字段；一次纠错仍相同。
2. 两次strict执行生成可选澄清问题，而稀疏shoppingGuide补丁没有重复mode；本地推荐可执行性校验拒绝，一次纠错仍未恢复。
3. 补充审计发现42个原主要评分通过的最终响应将goal写为字符串 `"null"`。保存响应的纯验证重放确认42个拟写入补丁都接受该值。主要评分覆盖不足，不代表完整语义通过；补充结论不回写原评分。

上述模型实验调用业务工具0次、持久化状态0次，没有把错误goal写进用户数据。strict只保证结构，不保证业务语义。没有修改生产代码来迁就这轮分数。

## 成本与完整性

- 共 {accounting['requests']} 次 DeepSeek 请求：96次基础抽取 + 5次结构纠错；不是重新运行失败attempt。
- 输入 {accounting['usage']['prompt_tokens']} / 输出 {accounting['usage']['completion_tokens']} / 总计 {accounting['usage']['total_tokens']} tokens。
- 101次请求均有回执和usage；实际max_tokens均4096，thinking均disabled，SDK重试均0。
- 96次执行全部保留；失败4次未删除；日志与原始请求/响应均按哈希关联。
- 全量回归使用测试自有Redis；模型key在该测试进程禁用。原共享服务与生产默认不变。
- P6首次导入错误属于本轮测试启动缺陷，不是产品测试失败；全量回归采用预检通过的显式PYTHONPATH，并保留原错误文件。
- 全量6项失败的根因是测试启动环境：4项因空key使SDK无法构造、1项因cwd导致相对pyproject路径错误、1项因仓库内临时目录改变external-output语义。纠正环境并禁止外部HTTP后，定点离线6/6通过，外部HTTP尝试0。原全量记录不变，不能声称一次完整全量运行全部通过。

## 结论边界与下一步

不能宣称Context改善了质量、token或延迟；P3/P4尚未执行。确认数据也不足以支持一般人群不劣效结论。没有人工评分，没有把合成数据叫做人类holdout。

下一步需先解决goal空值语义、稀疏补丁下可选澄清和定性偏好编造数值三处缺口，同时补足oracle与数据覆盖；另建修复/实验版本，不能覆盖本次失败。见 NEXT_REPAIR.md。默认开关保持不变。
'''
    with (HERE / "FINAL_REPORT.md").open("x", encoding="utf-8", newline="\n") as stream: stream.write(report)
    paths = sorted(p for p in HERE.rglob("*") if p.is_file() and not any(part in {"__pycache__", ".pytest_cache", "runtime"} for part in p.relative_to(HERE).parts)
                   and p.name not in {"SHA256SUMS.txt", "verification.json"})
    with (HERE / "SHA256SUMS.txt").open("x", encoding="utf-8", newline="\n") as stream:
        stream.write("".join(f"{file_sha(p)}  {p.relative_to(HERE).as_posix()}\n" for p in paths))
    print(json.dumps({"decision": final["decision"], "tests": full, "artifacts": len(paths)}), flush=True)


if __name__ == "__main__": main()
