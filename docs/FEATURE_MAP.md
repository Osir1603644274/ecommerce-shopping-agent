# 功能与代码索引

| 能力 | 主要实现 | 主要测试 |
|---|---|---|
| 受约束 ReAct 与 Harness | `agent/app/graph/nodes/react_policy.py`、`agent/app/harness.py`、`agent/app/validator.py` | `agent/tests/test_run_agent.py` |
| TaskState、ContextPack 与指代解析 | `agent/app/task_state.py`、`agent/app/context_pack.py`、`agent/app/reference_context.py` | `agent/tests/test_task_state.py`、`agent/tests/test_reference_context.py` |
| Checkpoint 与工具恢复 | `agent/app/graph/checkpoint.py`、`agent/app/graph/tool_inbox_v2.py`、`agent/app/graph/resume.py` | `agent/tests/test_run_agent.py`、`agent/evaluation/react_v1_durable_checkpoint_v2_20260901_v1` |
| 商品检索与重排 | `agent/app/domains/ecommerce/tools.py`、`agent/app/domains/ecommerce/ranking_contract.py` | `agent/tests/test_run_agent.py`、`agent/tests/two_stage_ranking_fixtures.py` |
| 长期记忆 | `agent/app/api/memory_bff.py`、`agent/app/memory_candidate_worker.py`、`backend/src/main/java/com/example/locallife/memory` | `agent/tests/test_memory_governance.py`、`backend/src/test/java/com/example/locallife/memory` |
| EvidenceResearchAgent | `agent/app/multi_agent_runtime_v2.py`、`agent/app/evidence_research_v1.py` | `agent/tests/test_run_agent.py` |
| TransactionAgent | `agent/app/transaction_agent/runtime.py`、`agent/app/api/transaction_agent.py` | `agent/tests/test_transaction_agent_runtime.py`、`agent/tests/test_transaction_agent_api.py` |
| 浏览器会话 BFF | `agent/app/api/commerce_demo.py`、`agent/app/static/commerce-demo.html` | `agent/tests/test_commerce_demo_api.py` |
| Java 订单、库存与支付 | `backend/src/main/java/com/example/locallife/ordering`、`inventory`、`payment` | `backend/src/test/java/com/example/locallife` |
| Outbox/Inbox 与搜索投影 | `backend/src/main/java/com/example/locallife/integration`、`search` | `OutboxInboxIntegrationTests`、`ProductChangedProjectionTests` |
| 候选池 Skill/MCP | `retrieval_judgment_pool_core`、`retrieval_judgment_pool_mcp` | `retrieval_judgment_pool_core/tests`、`retrieval_judgment_pool_mcp/tests` |

各模块通过显式接口连接：Agent 只调用 Java API，不直接操作交易表；检索候选池工具与在线导购链路相互独立。
