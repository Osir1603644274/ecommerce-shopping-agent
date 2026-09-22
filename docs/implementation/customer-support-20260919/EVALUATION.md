# 客服独立评测交付合同

状态：计量和汇总已实现，240场景数据、真实执行与人工复核尚未完成。不得将此文件或单元测试计数当作质量结果。

## 样本与版本

共240场景，120 dev、120 heldout；十类各24场景，每类12 dev与12 heldout：政策、商品证据、订单支付、物流催办、仅退款、退货退款、同规格换货、工单、归属与确认、故障恢复。

每条包括稳定id、category、split、taskKind(query/preview/workflow)、fixture、用户消息与确认/模拟事件步骤、预先登记的预期、oracle规则和familyId。同一模板家族不得跨split；记录合成来源。heldout表示冻结后不用于调参，不宣称人工金标或完全未知盲测。生成标签一律银标；人工复核另记人员/时间/判定理由。

三次执行使用同一冻结数据、模型配置和代码版本，新的业务fixture与requestId。用户等待与模拟器等待单列。运行中若修改代码，保留旧尝试并建立新版本，不混算不同版本。

## 外部观察与断言

观察器保存请求开始/结束、有效回答时刻、完整失败、每个模型/工具调用记录及业务标识。业务oracle从SQL原始订单、金额分摊、数量占用、库存、工单、事件与回执取证；禁止读取SUT的success字段直接判通过。

语义评分核对预先登记的意图、必须澄清的槽位、受限动作、事实及引用归属。金额/数量/身份/办理结果单列criticalAssertions，附原始证据位置。人工逐断言检查回答是否得到引用支持；未检查标为UNJUDGED，不用另一个模型冒充人工。

修正说明（2026-09-19）：旧runner的criticalAssertions实际上混合路由、完整性、无写入与事实检查。新汇总将其明确列为oracleChecks，不能解释为事实正确率，旧结果不重写。criticalFacts100改为读取逐断言独立复核导入的independentFactReview；缺审核或UNKNOWN均不能通过。

用户已选择无建设上下文的独立Agent复核。导入使用`import_human_review.py --reviewer-type AGENT`，只新增agentReview，原humanReview必须保持不变。审核表完整保留原答案/证据，逐断言提供原文quote、已登记evidencePath、JSON定位、支持/不支持/null、理由及critical布尔分类；导入器核验原始证据SHA、原文和结构，不能认证判断本身正确或把Agent审核变成人工金标。当前humanSupport95仍单独反映人工审核覆盖，不用Agent结果冒充其通过。

观察JSON每项字段：caseId、repetition、verdict(PASS/FAIL/UNJUDGED)、oracleEvidence、reasons、hardFailures、criticalAssertions(total/passed/complete)、firstContentMs、elapsedMs、modelMs、toolMs、userWaitMs、simulatorWaitMs、modelCalls、meteringComplete、humanReview(status/assertions/supported)。失败或缺失观察仍留在计划分母。

## 计量和报告

`agent/app/customer_support/metering.py`按固定官方报价快照、实际返回模型、UTC时段和缓存细分Token估算USD列表价；不是已结算账单。非官方端点、未知模型、缺用量、缺缓存细分或跨时段均显式报告未知，必要时给范围。已有usage的失败调用照计，未知不是零。

`scripts/customer_support/evaluation.py`仅汇总带外部证据的判定；输出总体/分类/每轮分母、Wilson区间、三轮均成功率、全部失败、硬失败、延迟分位、已知费用小计和未知覆盖。缺失任何调用计量或样本时不输出完整总费用。

额外授权正对照的模型调用单列evaluationControlUsage，并与用户请求usage去重后计入allExecutedModelUsage；对照耗时不伪装为用户请求时延，失败或缺usage同样计未知。Agent逐断言支持率单列agentGrounding/agentReviewGates，UNKNOWN留在全部断言及关键断言分母，humanGrounding保持独立；结构导入成功不等于质量验收通过。

最终门槛沿用STATUS.md：总体90%、每类80%、关键事实100%、人工支持95%、硬失败0；有效首响应P95≤5秒，查询≤30秒，预览≤45秒。样本数、三轮重复、计量覆盖、人工复核和真实浏览器交付必须分别验收。

依用户“派发没有上下文的窗口审核”的明确选择，新正式套件用`--reviewer-type AGENT`登记审核方法，支持率门槛仍为95%，关键事实仍须100%。acceptanceGates使用所登记方法的独立支持率，并要求三次重复；humanSupport95继续作为人工覆盖诊断保留false，不能称已人工验收。默认方法仍为HUMAN，必须显式选择AGENT，旧报告不改。审核结构校验不能替代外部逐断言判断。

## 当前草案

`evaluation/dataset-draft-001/`含240条、120家族及结构审计。每家族两变体有统计相关性，报告同时给出家族结果。43种fixture与47种驱动动作尚未全部实现。每条回答依据其回答时刻的SQL/事件快照评分；后续模拟动作改变状态后，不能拿新状态倒判旧回答。最终业务终态另用末次SQL快照核验。

`evaluation/dataset-draft-002/`保留240条的身份、分区与对话，只修正两条模型超时开发场景：以`preSteps`在首次chat之前注入故障，后置步骤仅重试原requestId；`expected.recoveryOutcome`预先登记恢复后的物流事实判分。001证据和数据不覆盖，002仍是未冻结银标草案。

重试计量由`trace_metering.py`汇总全部请求观察，按modelCallId/runId/toolCallId去重；保留失败调用以及未知usage。`elapsedMs`是各次客服HTTP请求耗时之和，`firstContentMs`是首次请求开始至首次COMPLETED回答的墙钟时间，包含恢复间隔；已完成响应重放不重置首响应。`simulatorWaitMs`记录首次请求后的驱动墙钟时间减去其中的客服HTTP请求时间（含BFF重启及驱动核对开销），`fixtureControlMs`另记首次请求前的驱动控制时间。用户等待在自动化脚本中为0。旧证据中未测量的恢复间隔不能据旧0值解释为没有等待。

## 串行批次入口

`scripts/customer_support/run_batch.py`要求显式指定`--split dev|heldout`和`--profile local|remote|logistics|receipt-retry`，为每批建立新的隔离数据库并管理18081 Java、18001 BFF生命周期。共享的隔离MySQL/Redis/库存服务须事先就绪；不接管已有端口，不重置数据。`--case-id`或`--limit`仅用于开发验证，不是完整验收。

`scripts/customer_support/run_suite.py --dataset <scenarios.jsonl> --runtime <新私有目录> --output <新证据目录>`按合同串行执行15批，共240×3项；加`--plan-only`只输出清单和指纹，不调用模型。每批前后检查数据、Java jar/源文件、Python应用/评测器和配置文件指纹，变化则停止。配置只保存摘要，不输出凭据。供应商模型别名背后的权重版本不由该摘要保证，仍须保留响应resolvedModel并说明供应商版本不透明的限制。

失败批次已有观察保留，缺失观察留在720项分母；无自动重启或覆盖旧attempt。STATE与REPORT分别标记执行状态和描述性结果，执行器不会自行宣布验收或将未人审答案改为人工通过。源代码变化后的结果不能混入固定版本正式汇总。

003仍为银标草案。CS-recovery-09-1已用于工程调试；后续保留集开发批次也需记录暴露，不再宣称未见盲测。正式执行完成不等于满足人工支持率及运行交接门槛。

业务拒绝的有效首响应采用独立判定：仅预先标注blocked_preview的场景，且SQL无写入、无交易/工单草稿、实际POST预览端点记录400/409/422拒绝、用户收到明确拒绝说明时，runner才将该requestId纳入supported_rejections。原trace FAILED及工具错误不改写，独立记录responseKind。任意模型故障、500、权限错误或无关404不适用；未知用量与失败调用仍保留。旧执行未记录工具状态码的证据不补造此证明。
