# 功能—源码—测试—证据索引

状态：`CURRENT_PUBLIC_INDEX`  
原则：源码证明“实现存在”，测试证明“合同成立”，正式证据限定“可以声称到哪里”。

| 功能 | 主要源码 | 主要测试 | 证据与允许口径 |
|---|---|---|---|
| 受约束 ReAct 与 Harness | `agent/app/graph/nodes/react_policy.py`、`agent/app/harness.py`、`agent/app/validator.py` | `agent/tests/test_run_agent.py`、`agent/tests/test_harness_integration.py` | `docs/REACT_V1_ARCHITECTURE_EXPERIMENT_RESULT_2026-08-27.md`；只按冻结场景描述架构选型 |
| TaskState / ContextPack / ReferenceContext | `agent/app/task_state.py`、`agent/app/context_pack.py`、`agent/app/reference_context.py` | `agent/tests/test_task_state.py`、`agent/tests/test_reference_context.py` | `agent/evaluation/reference_context_real_ui_v2_20260901_v1/REPORT.md`；有界指代、序号与跨任务隔离 |
| Checkpoint 与工具恢复 | `agent/app/graph/checkpoint.py`、`agent/app/graph/tool_inbox_v2.py`、`agent/app/graph/resume.py` | `agent/tests/test_graph_v2_checkpoint.py`、`agent/tests/test_checkpoint_recovery_inbox_v2.py` | `agent/evaluation/react_v1_durable_checkpoint_v2_20260901_v1/FINAL_EVIDENCE_AND_DECISION_2026-09-01.md`；不声称跨系统 Exactly-once |
| 商品检索与确定性约束 | `agent/app/domains/ecommerce/tools.py`、`agent/app/domains/ecommerce/ranking_contract.py` | `agent/tests/test_ecommerce_two_stage_search.py`、`agent/tests/test_ecommerce_ranking_contract.py` | `docs/evidence-product-retrieval.md`；ES 为派生索引，最终回查 Java/MySQL |
| 长期记忆治理 | `agent/app/api/memory_bff.py`、`agent/app/memory_candidate_worker.py`、`backend/src/main/java/com/example/locallife/memory` | `agent/tests/test_memory_governance.py`、`backend/src/test/java/com/example/locallife/memory` | `docs/experiments/shopping-memory-v14-2026-08-30/FINAL_EVIDENCE_AND_AUTHORITY_RESULT_2026-08-30.md`；确认、纠正、停用与删除闭环，默认边界以证据为准 |
| 条件式只读 Multi-Agent | `agent/app/multi_agent_runtime_v2.py`、`agent/app/evidence_research_v1.py` | `agent/tests/test_multi_agent_runtime_v2.py` | `agent/evaluation/multi_agent_runtime_v2_confirmation_20260902_v1/attempt001/RESULT.md`；不外推真人场景普遍提升 |
| TransactionAgent 与双确认 | `agent/app/transaction_agent/runtime.py`、`agent/app/api/transaction_agent.py` | `agent/tests/test_transaction_agent_runtime.py`、`agent/tests/test_transaction_agent_api.py` | [本轮自审](acceptance/commerce-demo-and-public-repository-governance-2026-09-03.md)；写操作仍由 Java 权威校验 |
| 浏览器安全会话 BFF | `agent/app/api/commerce_demo.py`、`agent/app/static/commerce-demo.html` | `agent/tests/test_commerce_demo_api.py` | JWT/refresh token 留在 Redis；浏览器只持 HttpOnly Cookie 和 CSRF token |
| Java 订单/库存/支付 | `backend/src/main/java/com/example/locallife/ordering`、`inventory`、`payment` | 对应 `backend/src/test/java/com/example/locallife` 目录 | `docs/experiments/java-backend-reliability-v1-2026-09-01/FINAL_RESULT.md`；事务、并发和故障结论，不是容量压测 |
| 分页批查与混合负载 | `OrderPageService`、`OrderMapper`；`scripts/backend-strengthening/mixed_workload.py` | `OrderPageIntegrationTests`、配对原始 HTTP/SQL | [V1](experiments/backend-strengthening-v1-2026-09-04/FINAL_RESULT.md)、[V2](experiments/backend-strengthening-v2-2026-09-05/RESULT.md)；同机描述性改善，不外推生产容量 |
| 双实例持久履约与支付边界 | `backend/src/main/java/com/example/locallife/fulfillment`、`PaymentService` | `PaymentBoundaryAuditTests`、`verify_faults.py`、`verify_load_faults.py` | [V3](experiments/backend-strengthening-v3-2026-09-05/RESULT.md)；8 场景/287 单、独立 SQL/SQLite 复算；本地渠道、默认关闭 |
| 多商品与按数量退款 | `CartOrderService`、`PartialRefundService`、`MoneyAllocation`、`V16__cart_orders_and_quantity_refunds.sql` | `CartTransactionIntegrationTests`、`verify_cart.py` | [V4](experiments/backend-strengthening-v4-2026-09-05/RESULT.md)；242 项测试及真实 MySQL/Kafka 有界验收；未接真实资金/物流 |
| Outbox/Inbox 与 ES 投影 | `backend/src/main/java/com/example/locallife/integration`、`search` | `OutboxInboxIntegrationTests`、`ProductChangedProjectionTests` | 事件可重放、消费者幂等与版本防旧覆盖；不等于端到端 Exactly-once |
| Agent→Java 完整交易 Demo | `scripts/commerce-demo.ps1`、`agent/app/static/commerce-demo.html` | `commerce-demo.ps1 smoke` 及上述定向测试 | [本轮自审](acceptance/commerce-demo-and-public-repository-governance-2026-09-03.md)；真实本地单实例闭环 |
| 候选池 Skill / MCP | `retrieval_judgment_pool_core`、`retrieval_judgment_pool_mcp` | 各模块测试与正式 runner | `evaluation/retrieval-judgment-pool-formal-v2-20260901/attempt001/RESULT.md`；显式 Skill、本地 STDIO MCP 有界通过，候选仍为 UNJUDGED |

## 禁止从索引外推

- 单元测试通过不等于线上容量、真实用户质量或生产默认切换。
- Demo 支付成功不等于真实支付渠道完成接入。
- 合成留出通过不等于 Multi-Agent 对所有问题都优于单 Agent。
- sealed 证据不得因当前源码变化被覆盖或原地重跑。
