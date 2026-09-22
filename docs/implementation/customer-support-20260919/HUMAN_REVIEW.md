# 人工复核操作

当前人工审核数为0。自动判分、模型辅助检查与人工审核分别记录；以下流程尚未执行完毕。

## 导出与审核

每个完整执行批次分别导出，不直接对包含三轮重复ID的suite顶层调用导出器。使用该批次的batch子目录及执行时相同的数据集：

```powershell
./.venv/Scripts/python.exe scripts/customer_support/export_human_review.py --run docs/implementation/customer-support-20260919/evidence/suite-fixed-001/01-r1-dev-local/batch --dataset docs/implementation/customer-support-20260919/evaluation/dataset-draft-006/scenarios.jsonl --output docs/implementation/customer-support-20260919/evidence/NEW_REVIEW_PACKET
```

批次尚未完成时不要导出。保留review.jsonl原件，审核人复制为review.completed.jsonl，只修改humanReview字段。逐条阅读所有不同回答，将金额、数量、身份、订单/售后状态、政策条件和办理承诺拆成可核验断言。

每条断言填写quote（回答逐字原文）、evidencePath（登记的证据路径）、evidenceLocation（具体字段或位置）、supported（true/false/null）与reason。支持依据必须在回答发生时已存在；后续模拟成功不能支持先前“已到账”的说法。null表示无法判断，仍进入断言分母。无回答或无事实断言时填写noAssertionsReason，不虚构支持断言。

完整检查该行后，填写reviewer、带时区的reviewedAt和status=REVIEWED。不得只挑自动PASS、只审成功轮次或用模型生成的审核冒充人工。当前合同要求全部720次观察完成复核，不按抽样结果外推。

## 导入与结果边界

```powershell
./.venv/Scripts/python.exe scripts/customer_support/import_human_review.py --packet docs/implementation/customer-support-20260919/evidence/NEW_REVIEW_PACKET --completed docs/implementation/customer-support-20260919/evidence/NEW_REVIEW_PACKET/review.completed.jsonl --output docs/implementation/customer-support-20260919/evidence/NEW_REVIEW_IMPORT
```

导入要求完整且唯一的(caseId,repetition)，核对原始回答、模板、观察及证据哈希，输出到新目录。导入成功只证明结构与证据绑定通过，不能证明审核者身份或实际进行了人工工作；需保留真实审核者确认。原始执行结果不改写。

最终汇总应使用三轮全部15个批次的审核结果，并保持720分母及原始自动判分、用量、缺失值。支持率为支持断言数/全部断言数，未审核或无法判断不能计为支持。人工支持率达到95%也不能替代关键事实100%、硬失败0、质量与时延等其他验收门槛。
