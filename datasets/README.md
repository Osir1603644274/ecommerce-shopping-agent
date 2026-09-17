# 我的数据集与 Benchmark

核对日期：2026-09-08。**以后从这里查看当前数据，不再按旧目录名猜版本。**

2026-09-13 补充：下方“2套”指仓库当前电商目录，**不包含外部普通搜索实验的大库**。外部实验实际使用 KuaiSearch-Lite 6,634,118 条＋MultiCPR 电商 1,002,822 条；它们没有自动成为商城可购买商品。已登记在 registry.json 的 `externalSearchCorpora`，并建立独立、可追溯的原文详情索引；见[修复计划](../docs/data/integration-repair-20260913/PLAN.md)与[结果](../docs/data/integration-repair-20260913/RESULTS.md)。原有数据、qrel 和模型均保留。

## 一次找到：先看这三项

新增开发材料：[手机型号知识覆盖表](knowledge/phone-v2/COVERAGE.md)，439条商品标题审核、171型号调查、425条事实/99型号。全量资料尚未完成，不能当作新benchmark；见[实施状态](../docs/acceptance/product-knowledge-mcp-20260908/README.md)。

- **[完整数据资产总账](ASSETS.md)**：33 个重点条目（含历史知识版本），按商品语料、单轮检索、多轮 Agent、跨会话记忆、外部/历史基准分类；每项都有实际文件、数量单位、来源性质和关系。
- **[全部扫描位置](ALL_LOCATIONS.md)**：F 盘项目及 D/E 盘已知实验材料共 202 个来源组、13,726 个结构化文件；包括未认定为独立数据集的配套材料和历史候选。
- **来源、重叠与版本关系（本地材料：`../docs/data/dataset-ledger-2026-09-08/relationships.json`）**：[本次盘点说明](../docs/data/dataset-ledger-2026-09-08/README.md)列出覆盖范围、排除目录、738 组同字节文件及验收结果。文件多不等于独立样本多。

重点多轮数据包括：行为集24场景65轮、真人8会话21轮、ReAct架构24场景33轮、任务状态14场景56轮、购物任务18场景53轮、Context长对话72/48/24轮。**这些均已登记，不能用24条单轮检索问题概括。**

## 当前电商数据底座：2 套

| 数据 | 实际拥有的内容 | 打开 |
| --- | --- | --- |
| KuaiSearch 多品类 | 46,079 商品、507 Query；商品检索语料，不代表全部相关性已人工标注 | 商品 documents.jsonl（本地材料：`current/kuaisearch/documents.jsonl`）、Query queries.jsonl（本地材料：`current/kuaisearch/queries.jsonl`） |
| KuaiSearch 二手手机子集 | 439 商品；保留筛选审计、结构化商品和合成参考价格 | 商品 catalog.jsonl（本地材料：`current/used-phone/catalog.jsonl`）、检索文档（本地材料：`current/used-phone/documents.jsonl`）、参考价格（本地材料：`current/used-phone/prices.jsonl`） |

手机 manifest 绑定的来源语料 SHA256 与上方多品类 documents 一致。439 手机是商品子集，不是新的独立原始数据源。价格为合成参考价，不能当真实市场价格。

## 当前手机检索 Benchmark

| 部分 | 内容 | 打开 |
| --- | --- | --- |
| 问题 | 24 条主检索：开发 12、验证 8、原划分测试 4；同文件另有 3 条能力边界问题 | 场景原文（本地材料：`benchmarks/phone-retrieval/scenarios/selected_scenarios_v1.jsonl`） |
| 多轮来源场景 | 另存 3 条；不能计成 24 条主检索，也不能自动当作完成的 Context benchmark | 多轮场景（本地材料：`benchmarks/phone-retrieval/scenarios/selected_multiturn_scenarios_v1.jsonl`） |
| 最终相关性标注 | 覆盖全部 24 Query，共 2,175 个 Query–商品对；Codex 辅助标注，非正式人工金标 | final_qrels.jsonl（本地材料：`benchmarks/phone-retrieval/review-and-score/final_qrels/final_qrels.jsonl`）、性质与回执（本地材料：`benchmarks/phone-retrieval/review-and-score/final_qrels/receipt.json`） |
| 评分 | 21 条可计分；另 3 条池内无相关商品。审阅链 ACCEPT，指标门与默认切换 HOLD | score_report.json（本地材料：`benchmarks/phone-retrieval/review-and-score/score_report.json`） |

**收集阶段 manifest 的“待建池/标注”是旧状态。当前完成情况应沿最终标注回执和评分报告判断。**原始文件不改写。

## 其他已拥有的 Benchmark

这些是不同能力的评测材料，不是新增商品库；不能混加 Query 数或分数，也尚未证明全部是上述 24 Query 的子集。

| 能力 | 材料与范围 | 当前入口 |
| --- | --- | --- |
| Context 当前首版 A/B/C | 单来源族、单条 48 轮开发会话；A/B/C 已完整，C 延迟门失败；取代旧的“C 未完成”交付状态 | [E 盘完整报告](E:/context-c-release002-20260906/FIRST_EDITION_ABC_REPORT.md)、来源冻结（本地材料：`../agent/evaluation/context_history_strategies_v1_20260905/first_edition_release002.json`） |
| Context 真实多轮回放 | 8 会话、21 轮；真实对话语义回放，有别于模型 Token 实验 | conversations.jsonl（本地材料：`../agent/evaluation/real_user_multiturn_replay_20260903_v7/conversations.jsonl`）、[结果](../agent/evaluation/real_user_multiturn_replay_20260903_v7/FINAL_EVIDENCE_AND_DECISION_2026-09-03.md) |
| Context 合成长历史对照 | 25 组合成压力场景；保留为另一类证据 | scenarios.jsonl（本地材料：`../agent/evaluation/context_raw_full_vs_compiled_v1_20260902_v1/scenarios.jsonl`） |
| 条件式 Multi-Agent | 8 条冻结合成留出；manifest 明确绑定当前 439 商品库 | scenarios.jsonl（本地材料：`../agent/evaluation/assets/multi_agent_runtime_v2_confirmation_20260902/scenarios.jsonl`）、合同（本地材料：`../agent/evaluation/multi_agent_runtime_v2_confirmation_20260902_v1/preregistered-manifest.json`） |
| 长期记忆自然候选首版 | 24 开发＋24 验收合成案例，6 条三会话验收轨迹；全局开关关闭 | [D 盘完整报告](D:/agent-experiments/memory-natural-v1-20260907/FIRST_EDITION_REPORT.md)、[冻结材料](D:/agent-experiments/memory-natural-v1-20260907/frozen002) |

外部盘原件只登记位置，不搬迁、不重跑；上表是持有材料与范围登记，不是重新验收全部实验。

## 配套材料与历史

- [support：来源标签、广度审查、检索原始结果、旧运行依赖](support/README.md)。它们不是另外几套当前商品底座。
- [历史归档](../data/_archive/2026-09-08/README.md)：16 个旧版本或阶段材料的实体已迁出原目录，逐文件保留。
- ESCI、Yelp 按本轮讨论退出当前电商评测主线；[原件与其他证据位置](HISTORY.md)仍保留，不删除已有实验。
- 旧 `data/benchmarks`、`data/derived`、`data/annotations` 路径及手机审阅旧路径现在是 Windows 目录联接；**它们指向同一份数据，不是额外副本。不要在旧路径上递归删除或编辑封存数据。**

## 如何保持清楚

机器清单：registry.json（本地材料：`registry.json`）。迁移前后文件哈希、旧新路径与清理记录：[整理验收](../docs/data/dataset-organization-2026-09-08/README.md)。

只读核验（不调用模型、不运行 benchmark）：

```powershell
python datasets/verify.py
python datasets/catalog.py
```

第一条检查已登记资产的内容和位置；第二条按相同扫描范围报告新增、删除、变化的候选文件，不自动写入或修改数据。

以后新增/替换数据或 benchmark，必须同步本页、assets.json 和 registry：登记来源、商品库版本、Query/会话数、标注性质、划分、原始结果位置。先运行 catalog.py 检查变化，人工确认角色与来源后新增盘点快照，不覆盖旧快照。新版本先登记再使用；不覆盖旧结果。尚未核实的数据关系写“未核实”，不能靠同属电商认定为同一基准。
