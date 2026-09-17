# 当前项目唯一权威入口

- 更新时间：2026-09-08
- 适用工作区：`F:\agent`
- 本文状态：`CURRENT`
- 工作树状态：受保护脏工作树；禁止 `reset/clean/stash/checkout/rebase/commit/push`

> 本文是“当前状态索引”，不是用一份总结覆盖原始证据。判断项目现状时先看本文，
> 再沿本文给出的唯一证据路径读取原始 manifest、receipt、score 和源码哈希。

## 1. 两个必须分开的状态轴

2026-09-09 商品知识/MCP有界功能验收完成：[最终证据](acceptance/product-knowledge-mcp-20260908/RESULT.md)。439标题审核、171型号调查、425事实覆盖99型号/199商品；223项相关测试、真实21轮与开发8轮双臂完成，MCP双端/并发/断连恢复验证通过。通用开关默认关闭，仅本地实验Demo开启；72型号无事实，游戏/拍照实测为0，不宣称全量知识、语义质量或生产排名通过。

2026-09-08 全景补充盘点：[数据资产总账](../datasets/ASSETS.md)登记30个重点资产条目，包含此前漏列的多轮场景；[全部位置](../datasets/ALL_LOCATIONS.md)覆盖已知F/D/E数据根的202个来源组、13,726个结构化候选文件。条目/文件/轮数不是独立样本数量。来源重叠与同family修订见关系证据（本地材料：`data/dataset-ledger-2026-09-08/relationships.json`），不搬迁或删除新材料、不重跑实验。

2026-09-08 数据整理补录：日常数据与 benchmark 入口改为 [datasets/README.md](../datasets/README.md)，机器清单为 datasets/registry.json（本地材料：`../datasets/registry.json`）。27 个目录实体集中/归档，旧路径以目录联接兼容；不改写冻结原件。手机 24 Query 已有 2,175 对 Codex 辅助最终标注和评分，不能沿用收集 manifest 的待标注状态。

同日核对到两个较新交付，补充旧索引的缺失：Context 首版 [E 盘 ABC 完整报告](E:/context-c-release002-20260906/FIRST_EDITION_ABC_REPORT.md) 已完成 C48，仍未通过 C 延迟门；[D 盘长期记忆自然候选首版](D:/agent-experiments/memory-natural-v1-20260907/FIRST_EDITION_REPORT.md) 已收口，全局关闭。下方旧版本证据保留其当时范围；本次仅核对材料和登记位置，不重跑实验。

文档生命周期：

- `CURRENT`：当前权威入口或当前实现。
- `ACTIVE`：仍在执行或维护。
- `SEALED`：已封存，禁止覆盖、改写或原地重跑。
- `EVIDENCE_ONLY`：只保留历史证据，不代表当前实现。
- `SUPERSEDED`：已被更新结论替代。

工程结论：

- `ACCEPT`：指定合同内通过。
- `BOUNDED_ACCEPT`：仅在明确样本、依赖和边界内通过。
- `HOLD`：不得扩大为生产完成、全局默认或更强结论。
- `IN_PROGRESS`：尚无最终结论。

生命周期与工程结论不得混写。例如 sealed attempt 可以是 `HOLD`；生产已经启用也不自动
等于完整生产验收 `ACCEPT`。

机器可校验的索引位于 `project-authority.json`（本地材料：`governance/project-authority.json`），校验命令：

```powershell
python scripts/validate_project_authority.py
```

`expected-authority-artifacts.json`（本地材料：`governance/expected-authority-artifacts.json`） 是独立完整性合同：它列出当前必须登记的权威条目和开放 blocker。校验器同时检查“应有条目是否存在”和“已登记条目是否内部一致”，不再只验证 registry 已收录内容。

## 2. 当前架构与默认状态

| 能力 | 当前实现 | 默认状态 | 当前结论 |
| --- | --- | --- | --- |
| Agent 控制策略 | LangGraph durable 上的受约束 `react_v1` | 网页默认；`fixed_v1` 为回滚/配对路径 | 已启用，但确认性生产效果门仍 `HOLD` |
| TaskState | Redis 中 session/task/revision 绑定的服务端权威状态 | 开启 | 定向验收通过；交易恢复与 Context 接线后当前全量 Agent suite 为 `3618 passed / 12 skipped / 0 failed`，属于当前源码/测试范围有界通过 |
| ReferenceContext | 绑定展示顺序、focus、上一批、CandidateScope 与 revision | 开启 | A2 两个真实网页 P1 已关闭：focus＋显式序号、比较后同 scope 复用均为 `BOUNDED_ACCEPT`；受影响回归 577/577 |
| ContextCompiler V1 | `RunContext → ContextItem → ContextCompiler → ModelContextView → Receipt` | `default-off` shadow | 旧 provider V4 因 P95 延迟门失败继续 HOLD；25 组合成长历史两臂 25/25 exact、总 Token -22.92%；真实多轮 V7 的 8 会话/21 轮答案与证据 21/21 字节一致、13 续问双 AI 盲评均平，但两臂 0 模型调用不能测 Token。结论 `BOUNDED_CONTEXT_SEMANTIC_FIDELITY_ACCEPT / HOLD_TOKEN_EFFICIENCY_AND_DEFAULT` |
| Context 历史策略首版 | A 全历史与 B Pack/View 的冻结开发 A/B；C 为阈值总结尝试 | 不切换生产默认 | release002 下 A/B 各 48 轮；B 总 Token 2,610,574→2,250,160（-13.81%），总耗时 +0.49%，P95 87.21→80.82s；96 条回答双评四维最大下降 0.03125/4。C 仅 43/48 轮并因共享源码漂移中止；结论 `DEVELOPMENT_AB_OBSERVED_THRESHOLDS_MET / INSUFFICIENT_EVIDENCE_C_INCOMPLETE`，formal/production 均 false |
| Multi-Agent Runtime V2 | Validator 证据缺口路由＋只读 EvidenceResearchAgent＋身份绑定消息＋Redis 原子合并＋确定性父端 | 仅证据缺口路径有界默认；失败回退单 Agent | 公开开发 research 为单 Agent 5/9、旧 MA2 7/9、修复后 9/9；冻结合成留出 8/8、P95 5.50s、原始子观测泄漏 0，判 `BOUNDED_SYNTHETIC_HOLDOUT_ACCEPT`；不授权交易写入、真人效果或全系统生产就绪 |
| 型号公开来源 ResearchAgent Gate 1 | `EXPANDED_PUBLIC_SOURCE_SET_V2` 的 24 个公开型号簇 | 不启用该 ResearchAgent 路线 | `GATE_1_REJECT`：0 个 Agent-eligible family、0 条 observation-dependent 轨迹；只授权确定性 EvidenceService/Workflow。它与已实现的条件式检索 Multi-Agent V2 是不同问题，不能互相覆盖 |
| 长期记忆 | MySQL 权威、Redis 投影、显式 consent/command、真实三会话全链路 | 全局默认关闭 | V18 `BOUNDED_REAL_FULL_CHAIN_V6_ACCEPT`；V14 的 425 用户未来行为上 C−A nDCG@10 `+0.007399`、95% CI `[+0.003720,+0.011641]`；仅离线关联，全局默认仍 `HOLD` |
| 记忆抽取 | 确定性前置门＋LLM 抽取＋歧义 fail-closed | 仅候选 worker | V15/V16 保留失败；V17 开放开发/确认集 24/24 有界通过，不是独立生产 authority |
| 商品检索 | ES Standard Top50＋MySQL 权威核验＋确定性过滤/重排 | 当前保留方案 | 选型 `HOLD` 于候选混合迁移；硬条件违规 0/831 |
| 型号证据层 | 标题型号规范化 → URL-only 来源注册表 → 权威/许可守卫 → 结构化型号事实 | `default-off` shadow，未接网页与排序 | 路由 `ACCEPT`；5 型号/22 商品事实切片仅 `BOUNDED_DEV_ACCEPT`；ResearchAgent V2 继续 `HOLD` |
| 商品知识库与 MCP 新主线 | V2：439标题、171型号调查、425事实 | 有界功能验收；仅本地实验Demo开启 | [最终记录](acceptance/product-knowledge-mcp-20260908/RESULT.md)：223测试、真实21轮/开发8轮双臂、MCP双端一致。来源覆盖和自然语言取舍仍有限，不作生产或总体质量提升声明 |
| 交易履约 | 无模型 TransactionWorkflow → Spring Boot/MySQL 权威交易核心 | 高风险写入仅后端；TransactionAgent 默认关闭 | Agent 不拥有订单、库存、支付等写权限；危险窗口以不可变命令摘要、Java 权威状态回查和同键至多一次补偿闭合，12 个冻结场景通过，判 `BOUNDED_TRANSACTION_COMMAND_RECOVERY_ACCEPT` |
| 电商全链路 Demo / 公开 GitHub | FastAPI BFF＋TransactionAgent＋Java 交易后端；允许列表快照＋独立 SHA/密钥检查 | 全部演示开关默认关闭；仅本地脚本显式开启 | 真实浏览器链路完成检索、双确认下单/支付、模拟回调与 Agent 回查 `PAID`；公开快照 v13 发布后复核 0 错配，GitHub CI 与 `v1.0.1` Release 通过，判 `PUBLIC_REPOSITORY_AND_BROWSER_DEMO_ACCEPT`；不等于真实支付或生产就绪 |
| Java 后端可靠性 | 模块化单体、缓存/限流/订单补偿/秒杀/Outbox | 当前实现 | `BOUNDED_BACKEND_RELIABILITY_ACCEPT`；此前验收后端 `201/201`、网关 `4/4` |
| 订单查询与可靠异步履约 V1 | 游标分页＋批查；持久任务、专用 Kafka、租约/fence、仓库回查、退款库存恢复 | 履约默认关闭，专用环境显式开启 | `BOUNDED_BACKEND_STRENGTHENING_ACCEPT / HOLD_PRODUCTION_DEFAULT`；JDK 17 常规 216/216、真实链路 10/10；1万/10万/100万 SQL 对照；4703 次 HTTP 读取零错误，仅本机描述性结果 |
| 订单混合负载与锁等待 V2 | 同接口 N+1 控制/批查对照；真实创建、回调、取消与库存/履约核验 | 业务源码及生产默认未改，独立服务已停止 | `BOUNDED_MIXED_WORKLOAD_AND_LOCK_RECOVERY_ACCEPT`；四对分页 P95/P99 变化率中位数 −47.95%/−34.65%；主要对照与锁场景 17,988 请求、1,311 单、0 HTTP 错误；仅同机闭环客户端描述性证据 |
| 后端独立审计与双实例故障 V3 | 支付模拟入口事务修复；逐单证据门禁；真实 Redis/Kafka/行锁/仓库延迟 | 履约默认关闭，专用服务已停止 | `BOUNDED_TWO_INSTANCE_FAULT_ACCEPT`；8 项主要场景 287 单、586 组 Outbox/Inbox 对齐；四段持续负载 1200 HTTP 零错误；独立 SQL/SQLite 复算通过。同机有界，本地渠道，恢复核对为观察上界，非生产 RTO |
| 多商品与按数量退款 V4 | 多 SKU 购物车、整数分优惠、未派发数量退款、渠道回执对账、命令版本 | 履约默认关闭；本地渠道；专用服务已停止 | `BOUNDED_MULTI_ITEM_AND_QUANTITY_REFUND_ACCEPT`；Java 242/242；真实 MySQL 14 单中 12 逆序并发无死锁，101+100+501 分守恒，32 Outbox/64 双消费收据一致。真实资金、已出库退货、长期容量继续 HOLD |
| Spring Cloud 拆分 | Gateway＋Catalog/Search＋Trade；Feign/Bulkhead/CircuitBreaker | 可验证部署形态，模块化单体仍默认 | V5 正式场景 10/10、后端 197/197、网关 4/4；同源对照中拆分 P50/P95 延迟增加 104.65%/11.88%，启动增加 158.64%，故不切默认 |
| Redis Cluster | 同机 3 master＋3 replica、Cluster-aware 客户端、hash tag、MOVED/故障转移 | 未采用 | 业务不变量与故障恢复有界通过，但单热点/48 Key 中位吞吐分别下降 7.18%/15.53%，严格结论 `HOLD_REDIS_CLUSTER` |
| 候选池 Skill/MCP | 10 路候选池、显式 Skill、本地 STDIO MCP | 非生产默认 | V2 结构/联合有界通过；V4.1 真实 ES+BGE 核心有界通过；质量仍无真人标签 |
| 隐式 Skill 路由 | 隔离探针与随机 attestation | 未启用 | V9 的 48 次调用因 Windows 只读命令策略导致正文读取失败；V10 新根目录控制仍失败并正确阻止正式调用，平台可观测性解除前停止迭代 |
| ReAct Checkpoint V2 | 当前控制策略上的真实 Redis/多进程、篡改、revision、lease/fence 矩阵 | 评测已完成；`react_v1` 默认与 `fixed_v1` 回滚角色不变 | `BOUNDED_DURABILITY_ACCEPT_WITH_SEPARATE_REMEDIATION`；生产就绪与默认切换继续 `HOLD` |

## 3. 当前唯一证据路径

| 主题 | 当前入口 | 可使用边界 |
| --- | --- | --- |
| 总待办与状态 | [`2026年09月唯一待办清单.md`](2026年09月唯一待办清单.md) | 当前细项状态；不能替代原始 attempt |
| ReAct 架构实验 | `REACT_V1_ARCHITECTURE_EXPERIMENT_RESULT_2026-08-27.md`（本地材料：`archive/experiments/react-v1/REACT_V1_ARCHITECTURE_EXPERIMENT_RESULT_2026-08-27.md`） | 描述性架构选择，不单独授权生产切换 |
| Context/Multi-Agent V1（历史） | [`acceptance/context-multiagent-v1-implementation-and-public-pilot-2026-08-31.md`](acceptance/context-multiagent-v1-implementation-and-public-pilot-2026-08-31.md) | V1 压缩与无质量增益的历史证据；不得覆盖新的 raw-full Context 配对和 Multi-Agent V2 开发结论 |
| Context 原始全量历史配对 | [`../agent/evaluation/context_raw_full_vs_compiled_v1_20260902_v1/FINAL_EVIDENCE_AND_DECISION_2026-09-02.md`](../agent/evaluation/context_raw_full_vs_compiled_v1_20260902_v1/FINAL_EVIDENCE_AND_DECISION_2026-09-02.md) | 25 组确定性长历史压力场景中两臂均 25/25 exact，Prompt/总 Token -26.43%/-22.92%，P95 比 0.9092；不等于真人对话或生产全域质量 |
| Context 真实多轮配对 V7 | [`../agent/evaluation/real_user_multiturn_replay_20260903_v7/FINAL_EVIDENCE_AND_DECISION_2026-09-03.md`](../agent/evaluation/real_user_multiturn_replay_20260903_v7/FINAL_EVIDENCE_AND_DECISION_2026-09-03.md) | 8 会话/21 轮答案、完整对话和公开证据 21/21 字节一致；13 个续问两次独立 AI 盲评均平。两臂 0 模型调用，不能声称 Token 降低、真人审查或默认切换 |
| Context 历史策略首版 | [`../agent/evaluation/context_history_strategies_v1_20260905/FIRST_EDITION_REPORT.md`](../agent/evaluation/context_history_strategies_v1_20260905/FIRST_EDITION_REPORT.md) | 冻结 release002 的单来源族、固定顺序开发样本：A/B 各 48 轮，B Token -13.81%、总耗时 +0.49%，双评四维最大下降 0.03125/4；C43 源码漂移中止，不得宣称完整 A/B/C、正式验收或生产切换 |
| 消息机制选型与修复 | [`adr/messaging-selection-2026-09-06.md`](adr/messaging-selection-2026-09-06.md)、[`acceptance/messaging-remediation-2026-09-06.md`](acceptance/messaging-remediation-2026-09-06.md) | Kafka＋Outbox/Inbox 承担领域事件，MySQL 请求表承担秒杀受理权威；Java 256/256、真实 MySQL/Redis 6/6。单 broker、AOF everysec、模拟消费者验证不等于高可用或全部真实 broker 崩溃窗口 |
| Multi-Agent V2 开发证据 | [`../agent/evaluation/runs/context_multiagent_public_pilot_v3_attempt001_remediation001/FINAL_EVIDENCE_AND_DECISION_2026-09-02.md`](../agent/evaluation/runs/context_multiagent_public_pilot_v3_attempt001_remediation001/FINAL_EVIDENCE_AND_DECISION_2026-09-02.md) | 全量 29/33→31/33、research 5/9→7/9、父最终上下文均值 -23.17%；CI 跨零且 P95 变慢，不授权一般优越或默认切换 |
| Multi-Agent Runtime V2 公开开发修复 | [`../agent/evaluation/multi_agent_runtime_v2_remediation_20260902_v1/attempt001/RESULT.md`](../agent/evaluation/multi_agent_runtime_v2_remediation_20260902_v1/attempt001/RESULT.md) | 既有 9 个公开 research 场景由 CTX1b 5/9、旧 MA2 7/9 到确定性父端 9/9；是 post-hoc 开发修复，不是 untouched confirmation |
| Multi-Agent Runtime V2 合成留出 | [`../agent/evaluation/multi_agent_runtime_v2_confirmation_20260902_v1/attempt001/RESULT.md`](../agent/evaluation/multi_agent_runtime_v2_confirmation_20260902_v1/attempt001/RESULT.md) | 冻结合成留出 8/8；8 次真实子 Agent 模型调用与 8 次只读工具调用，P50/P95 3.40/5.50s、usage 8/8 完整；不证明真人效果、普遍优于或全系统生产就绪 |
| Multi-Agent Runtime V2 有界默认收据 | [`../agent/evaluation/multi_agent_runtime_v2_default_enablement_20260902/FINAL_VERIFICATION.md`](../agent/evaluation/multi_agent_runtime_v2_default_enablement_20260902/FINAL_VERIFICATION.md) | 证据缺口只读子链开启后的定向 276/276 与全量 3593 passed、12 skipped、0 failed；子链异常回退，不具备交易写权限；不等于真人网页或全系统生产 ACCEPT |
| Agent 全量 suite 收口 | [`../agent/evaluation/full_agent_suite_closure_v1_20260902/FINAL_EVIDENCE_AND_DECISION_2026-09-02.md`](../agent/evaluation/full_agent_suite_closure_v1_20260902/FINAL_EVIDENCE_AND_DECISION_2026-09-02.md) | 冻结 444 个 Python 源码/测试文件，唯一执行 3585 passed、12 skipped、0 failed；不证明外部依赖或生产就绪 |
| 型号来源确定性路由 | [`../evaluation/used-phone-model-evidence-v1/RESULT.md`](../evaluation/used-phone-model-evidence-v1/RESULT.md) | 439/439 解析等价、24/24 路由、具体机况 fail-closed；不证明已取得型号事实或 Multi-Agent 有效 |
| 结构化型号事实开发切片 | [`../evaluation/used-phone-model-facts-dev-v1/RESULT.md`](../evaluation/used-phone-model-facts-dev-v1/RESULT.md) | 5 个型号事实、22 个精确型号商品；仅合同与解析开发证据，不授权用户接线或生产排序 |
| 长期记忆架构边界 | [`experiments/shopping-memory-v14-2026-08-30/FINAL_EVIDENCE_AND_AUTHORITY_RESULT_2026-08-30.md`](experiments/shopping-memory-v14-2026-08-30/FINAL_EVIDENCE_AND_AUTHORITY_RESULT_2026-08-30.md) | sealed 架构证据；全局默认继续 HOLD |
| 长期记忆真实全链路 V18 | `../agent/evaluation/shopping_memory_v18_real_full_chain_20260830/attempt006/report.json`（本地材料：`../agent/evaluation/shopping_memory_v18_real_full_chain_20260830/attempt006/report.json`） | 42/42 检查、439 商品、真实 MySQL/Redis/ES/Java/DeepSeek 三会话链；不授权全局默认、因果质量或全品类 |
| 交易命令恢复 V1 | [`../agent/evaluation/transaction_command_recovery_v1_20260902_v1/attempt001/RESULT.md`](../agent/evaluation/transaction_command_recovery_v1_20260902_v1/attempt001/RESULT.md) | 12 个冻结场景、含真实 Redis 重启的 Python 恢复套件与 Java Spring/H2 6/6；只授权不可变命令保留、权威对账与同键至多一次补偿，不授权跨系统 exactly-once、生产就绪或默认启用 |
| 记忆抽取 V15/V16/V17 | `../agent/evaluation/shopping_memory_extraction_v17_20260830/attempt001/report.json`（本地材料：`../agent/evaluation/shopping_memory_extraction_v17_20260830/attempt001/report.json`） | V15/V16 失败不覆盖；V17 24/24 开放开发/确认集有界通过，不是独立生产 authority |
| Java 后端可靠性 | [`experiments/java-backend-reliability-v1-2026-09-01/FINAL_RESULT.md`](experiments/java-backend-reliability-v1-2026-09-01/FINAL_RESULT.md) | 正确性、并发和故障证据；不是容量/QPS 压测 |
| 后端强化 V1 | [`订单查询与可靠履约最终结果`](experiments/backend-strengthening-v1-2026-09-04/FINAL_RESULT.md) | 不依赖 Agent；真实 MySQL/Kafka/本地仓库故障恢复与有界查询证据，原失败保留 |
| 后端强化 V2 | [`混合负载与数据库锁等待`](experiments/backend-strengthening-v2-2026-09-05/RESULT.md) | 四对同接口比较、3 秒锁阻塞、连接池/积压/业务终态原件；受认证维护干扰的原配对单列保留，主要配对完整重做 |
| 后端强化 V3 | [`独立审计与双实例故障`](experiments/backend-strengthening-v3-2026-09-05/RESULT.md) | 支付事务与分析器修复、固定 8 场景、287 单逐单 SQL/SQLite 复算；持续四类故障，恢复上界不当作精确 RTO |
| 后端强化 V4 | [`多商品与按数量退款`](experiments/backend-strengthening-v4-2026-09-05/RESULT.md) | 真实 MySQL 迁移、并发退款、锁升级死锁和双消费者闭环；失败原件与修复前后对照均保留，生产默认不变 |
| Spring Cloud V5 | [`experiments/spring-cloud-bounded-split-v5-server-concurrency-evidence-2026-09-01/FINAL_EVIDENCE_AND_DECISION_2026-09-01.md`](experiments/spring-cloud-bounded-split-v5-server-concurrency-evidence-2026-09-01/FINAL_EVIDENCE_AND_DECISION_2026-09-01.md) | 10/10 正式场景、197/197＋4/4；服务端峰值 7，不能写 200 服务端并发或生产默认切换 |
| 单体与 Spring Cloud 同源决策 | [`experiments/monolith-vs-spring-cloud-decision-2026-09-02/FINAL_EVIDENCE_AND_DECISION_2026-09-02.md`](experiments/monolith-vs-spring-cloud-decision-2026-09-02/FINAL_EVIDENCE_AND_DECISION_2026-09-02.md) | 商品响应 50/50 字节一致；拆分带来更高延迟、启动时间与复杂度，仅保留为有界部署形态 |
| Redis Cluster 有界选型 | [`experiments/redis-cluster-bounded-v1-2026-09-02/FINAL_EVIDENCE_AND_DECISION_2026-09-02.md`](experiments/redis-cluster-bounded-v1-2026-09-02/FINAL_EVIDENCE_AND_DECISION_2026-09-02.md) | 同机 3 主 3 从的业务不变量、MOVED 与故障恢复通过，但吞吐未获益，当前不采用 Cluster |
| 候选池 Skill/MCP V2 | [`../evaluation/retrieval-judgment-pool-formal-v2-20260901/attempt001/RESULT.md`](../evaluation/retrieval-judgment-pool-formal-v2-20260901/attempt001/RESULT.md) | 显式 Skill、本地 STDIO MCP 与联合字节一致性有界通过；候选仍 UNJUDGED |
| 真实 ES+BGE 核心 V4.1 | `../evaluation/retrieval-judgment-pool-v4-1-remediation-20260901/attempt001/result.json`（本地材料：`../evaluation/retrieval-judgment-pool-v4-1-remediation-20260901/attempt001/result.json`） | ES 8.17.6 与 BGE-small-zh-v1.5 双次只读重放；ColBERT、质量和生产继续 HOLD |
| 隐式 Skill V9/V10 | [`../evaluation/retrieval-judgment-pool-v10-readable-loader-20260902/FINAL_EVIDENCE_AND_DECISION_2026-09-02.md`](../evaluation/retrieval-judgment-pool-v10-readable-loader-20260902/FINAL_EVIDENCE_AND_DECISION_2026-09-02.md) | V9 封存 48 调用失败证据；V10 新控制仍被 Windows CLI 只读策略拒绝并阻止正式调用，隐式路由保持 HOLD |
| 旧 Checkpoint 真实恢复 | `../agent/evaluation/runs/checkpoint_graph_v2_durable_1x_20260825_attempt001/score.json`（本地材料：`../agent/evaluation/runs/checkpoint_graph_v2_durable_1x_20260825_attempt001/score.json`） | 危险窗口 PASS；篡改/revision 矩阵未在该 attempt 执行；目录 sealed |
| 新 Checkpoint V2 | [`../agent/evaluation/react_v1_durable_checkpoint_v2_20260901_v1/FINAL_EVIDENCE_AND_DECISION_2026-09-01.md`](../agent/evaluation/react_v1_durable_checkpoint_v2_20260901_v1/FINAL_EVIDENCE_AND_DECISION_2026-09-01.md) | Layer A 130/130；12 个篡改用例全部 fail closed；原 attempt `FAIL` 未改写，独立 remediation 3/3；不授权生产默认切换、跨系统 exactly-once 或集群恢复声明 |
| Agent 运行路径收口 | [`acceptance/agent-runtime-path-convergence-2026-09-01.md`](acceptance/agent-runtime-path-convergence-2026-09-01.md) | 正式/回滚/实验路径已显式分层；270 项组合回归通过，但不得宣称全量 suite 绿色 |
| Context 真实 provider 配对 V3（历史） | [`../agent/evaluation/context_compiler_provider_paired_v1_20260901_v3/FINAL_EVIDENCE_AND_DECISION_2026-09-01.md`](../agent/evaluation/context_compiler_provider_paired_v1_20260901_v3/FINAL_EVIDENCE_AND_DECISION_2026-09-01.md) | 真实 Token 降低、确定性重编译 130/130；recentReference fidelity 门失败，保留为历史证据 |
| Context 真实 provider 配对 V4 | [`../agent/evaluation/context_compiler_provider_paired_v1_20260901_v4/FINAL_EVIDENCE_AND_DECISION_2026-09-01.md`](../agent/evaluation/context_compiler_provider_paired_v1_20260901_v4/FINAL_EVIDENCE_AND_DECISION_2026-09-01.md) | 65 个旧案例＋4 个 A2 脱敏真实链路；138/138 调用、两臂 fidelity 69/69、输入 Token -4.83%，但 P95 延迟门失败，最终 `HOLD`，默认不切换 |
| Multi-Agent Gate 1 来源可行性 V2 | [`../evaluation/context-multiagent-v2-feasibility-v2/phase1b-execution-report-v2.md`](../evaluation/context-multiagent-v2-feasibility-v2/phase1b-execution-report-v2.md) | 准确范围为 `GATE_1_REJECT / EXPANDED_PUBLIC_SOURCE_SET_V2`；24 簇中官方精确入口 22、独立受控性能入口 11，但可信物理商品绑定、真实动态轨迹和 Agent-eligible family 均为 0；不得实现 ResearchAgent V2 或声称 Multi-Agent 普遍无效 |
| ReferenceContext 真实网页 V1 | [`../agent/evaluation/reference_context_real_ui_v1_20260901_v1/REPORT.md`](../agent/evaluation/reference_context_real_ui_v1_20260901_v1/REPORT.md) | 三项 `BOUNDED_ACCEPT`、两项 `HOLD_P1`；该报告保留修复前 487 passed / 3 failed，不覆盖后续 A1 结果 |
| ReferenceContext 真实网页 V2 | [`../agent/evaluation/reference_context_real_ui_v2_20260901_v1/REPORT.md`](../agent/evaluation/reference_context_real_ui_v2_20260901_v1/REPORT.md) | 四轮真实网页关闭两个 A2 P1，受影响回归 577/577；仅为有界接受，不代表 A3、全量 Agent suite 或生产级验收 |
| 主线 A1 三项回归收口（历史） | [`../agent/evaluation/mainline_a1_regression_closure_v1_20260901/REPORT.md`](../agent/evaluation/mainline_a1_regression_closure_v1_20260901/REPORT.md) | 三项旧测试合同对齐与 490/490 是后续全量 suite 归零前的阶段证据，现为 `EVIDENCE_ONLY` |
| 简历历史证据矩阵 | `RESUME_EVIDENCE_MATRIX_2026-09-02.md`（本地材料：`archive/career/RESUME_EVIDENCE_MATRIX_2026-09-02.md`） | 2026-09-03 前的证据汇总；最新一页两项目展示与学习范围取舍见 V27 记录，原始实验包仍是数字权威 |
| 当前面试学习指南 | `INTERVIEW_LEARNING_GUIDE_2026-09-02.md`（本地材料：`archive/career/INTERVIEW_LEARNING_GUIDE_2026-09-02.md`） | 用于逐项补课和复现，不替代实验结果 |
| 简历 V13 交付收据 | `RESUME_V13_DELIVERY_RECEIPT_2026-09-03.md`（本地材料：`archive/career/RESUME_V13_DELIVERY_RECEIPT_2026-09-03.md`） | 记录最终 DOCX/PDF 路径、SHA256 与版面验收，不替代各项实验 authority；旧交付收据保留为历史 |
| 最新交付 Agent 后端简历 V31 | V31 交付记录（本地材料：`career/RESUME_V31_MESSAGING_CONTEXT_DELIVERY_RECEIPT_2026-09-06.md`） | Word/PDF 均一页、正文 11 磅；以 V29 均衡版为底稿，写入冻结 Context A/B 与消息可靠性修复，项目高度 51.14% : 48.86%。C43 中止和 formal/production false 均保留在证据边界；V28–V30 继续保留，不替代原始实验依据 |

## 4. 简历当前允许使用的结论

- 2026-09-05 用户确认简历与网申拆为两个独立项目，Java 保留 Agent 与交易闭环。两者仍在同一仓库、通过接口协作，不新增公司经历或独立开发时间。经历 V21 两页撤回、V22 Java 核心不足、V24/V25 的约 4:6 试排后，用户明确取消 4:6，以指定原稿的问题/解决/数据为核心；新功能尚未学会，空间不足则少加。V26 为“智能购物 Agent 系统”“Java 电商交易平台”，一页、11 磅正文，Agent 七条与 Java 原有五条加查询优化；其余新成果暂缓纳入。2026-09-06 用户要求填满页面，V27 在此基础上补充已有机制与证据，并展开查询过程，仍为一页且没有新增业务功能，不推断个人掌握。

- 可以写 ReAct 与固定 PAE 的架构演进、24 场景对照及其质量/延迟取舍；不能写“统计证明全面优于”。
- 可以写三层 Context 证据：25 组合成长历史两臂均 25/25 exact、总 Token -22.92%；真实多轮 V7
  的 8 会话/21 轮答案和证据 21/21 字节一致、13 个续问双 AI 盲评均平；冻结 release002 的新版
  A/B 各 48 轮，B 总 Token -13.81%、总耗时 +0.49%、双评四维最大下降 0.03125/4。新版仍是单来源族、
  固定顺序开发样本，C 只完成 43/48 轮；不得宣称完整 A/B/C、真人金标、正式验收或生产切换。
- 可以写 Multi-Agent Runtime V2 的 Coordinator→只读 EvidenceResearchAgent→Redis 原子合并→确定性父端链路；
  公开开发 research 为 5/9→7/9→9/9，冻结合成留出 8/8、P95 5.50s、原始子观测泄漏 0。
  必须注明前者是 post-hoc 开发修复、后者是合成留出；不能写真人效果、普遍优于或全系统生产就绪。
- 可以写 Java 模块化单体的事务、缓存、Outbox/Inbox 与秒杀不变量；Spring Cloud V5 可写正式场景
  10/10、后端 197/197、网关 4/4，以及 200 个客户端任务争 50 库存时 50 成功/150 售罄/0 超卖；
  必须同时说明服务端购买处理器峰值为 7，不得写 200 服务端并发、QPS 或容量提升。
- 可以写单体/拆分同源对照发现商品响应 50/50 字节一致且拆分能隔离目录故障；不得回避拆分 P50/P95
  延迟增加 104.65%/11.88%、启动增加 158.64%，所以模块化单体继续默认。
- Redis Cluster 只能作为负向选型经验：同机 3 主 3 从通过业务不变量与故障转移，但单热点/48 Key 中位吞吐
  分别下降 7.18%/15.53%，当前不采用；不能写成性能提升或生产集群能力。
- 可以写 Checkpoint V2 的真实 Redis/独立进程故障注入：130/130 恢复用例通过、12/12 篡改拒绝，
  重复/缺失工具副作用、旧 Worker 覆盖和 TTL 泄漏均为 0；若写 42.182/75.475 ms，必须注明是进程内 RTO P50/P95。
- 原正式 attempt 仍为 `FAIL`，独立 remediation 只证明陈旧引用模型路径 3/3 可达；不得写成一次全绿实验、通用模型质量、跨系统 exactly-once 或生产就绪。
- 可以写当前 Agent suite `3618 passed / 12 skipped / 0 failed`；旧冻结包的 3585/12
  仍保留为历史 authority。新数字仅证明当前源码与测试集合，不证明外部依赖或生产就绪。
- 可以写已实现零模型、零联网的型号来源路由，并在 439 条目录上达到 439/439 解析等价；
  5 型号事实切片只能写为开发验证，不能写成推荐质量提升、设备级验机或生产排名能力。
- 长期记忆可写 MySQL 权威、Redis 投影、显式 consent/command 与 V18 真实三会话全链路 42/42；
  也可写 425 用户未来行为上 C−A nDCG@10 `+0.007399`、95% CI `[+0.003720,+0.011641]`；
  这是单点击 cohort 离线关联，不得写因果提升、显式偏好、生产全量启用或独立确认全绿。
- 可以写交易危险窗口以不可变命令摘要、Java 权威状态回查和原幂等键至多一次补偿闭合；未决必须
  保持 `UNKNOWN`。不得写分布式原子性、跨系统 exactly-once、生产就绪或默认启用。
- 候选池可写 10 路检索器、显式 Skill、本地 STDIO MCP、真实 ES 8.17.6 与 BGE-small-zh-v1.5；
  不得写真人 qrels 质量、真实 ColBERT、隐式默认触发或生产安装；V9/V10 只用于说明平台限制，不能包装成成功。

### 历史总览文档的当前定位

以下文件继续保留，但统一标记为 `EVIDENCE_ONLY`，不能单独裁决当前状态：

- `PROJECT_STATUS.md`
- `ARCHITECTURE.md`
- `BENCHMARK.md`
- `EXPERIMENTS.md`
- `CURRENT_HANDOFF.md`
- `INTERVIEW_EVIDENCE.md`
- `EXECUTION_PLAYBOOK.md`

它们包含有价值的历史过程和旧阶段数据；若与本文或当前源码冲突，不得通过改写历史来消除冲突，
而应在本文登记当前结论，并保留旧证据原貌。

## 5. 当前阻塞与整改顺序

机器可读 blocker 历史固定保留五项：前三项已在 A1 关闭，后两项已在 A2 关闭：

1. `[RESOLVED] durable-terminal-replay-history-read`：完整身份绑定 receipt 已证明在 history 读取前识别。
2. `[RESOLVED] final-answer-test-route-boundary`：旧测试显式选择非 durable 边界，生产路由不变。
3. `[RESOLVED] evidence-projection-contract-assertion`：断言已要求 checks refs 与 title/brand refs 同时保留。
4. `[RESOLVED] reference-focused-plus-explicit-ordinal-ui`：durable 与 stream 共用服务端解析；修复后真实网页按 focus 卡 2＋显式第 3 项完成比较。
5. `[RESOLVED] post-compare-same-scope-recommendation`：source Executor receipt 与比较子集分别校验；修复后同 scope 追问无 stale/identity 错误。

整改顺序：

1. 三项 Agent 回归及其后续全量 suite 已完成有界收口；旧冻结包 3585/12 保留，当前全量为 3618 passed、12 skipped、0 failed。
2. 两个 ReferenceContext 真实网页 P1 已有修复前失败与修复后脱敏证据包；A2 到此收口。
3. Context provider V4 的延迟 HOLD、合成长历史 raw-full 配对和真实多轮 V7 语义等价证据同时保留；默认关闭，不能用 V7 的 0 模型快路径覆盖旧 provider 失败。
4. Multi-Agent Runtime V2 已取得公开开发修复 9/9 与冻结合成留出 8/8，证据缺口只读路径有界默认开启；真人多轮、真实网页与全系统生产授权仍待独立证据，不阻塞当前投递。
5. 显式 Skill/MCP 已收口；隐式路由停在 V10 平台控制 HOLD，不再无差别增加版本。
6. Spring Cloud 与 Redis Cluster 已完成当前选型：模块化单体、Redis 单节点继续默认。
7. 交易危险窗口已完成权威对账与幂等补偿闭环；后续仅在新增真实业务或部署需求时扩展生产级多主机、真人 qrels 与长期运维证据。

## 6. 更新规则

- 新能力只有在正式结果落盘后才能从 `IN_PROGRESS/HOLD` 提升。
- sealed 目录永不原地修补；修复必须创建新版本和新 attempt。
- 每次更新本文时必须同步 `project-authority.json` 并运行校验器。
- README、交接文档和简历若与本文冲突，以当前源码配置和本文指向的原始证据为准，并立即登记为待修复漂移。
