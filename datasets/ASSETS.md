# 数据资产总账

新增商品知识资产：[手机知识phone-v2](knowledge/phone-v2/COVERAGE.md)。439条标题审核记录、171型号调查、425条已收录事实覆盖99型号；不是金标或完整知识覆盖。机器清单已纳入，总计33个重点资产条目（含历史V1与当前V2）。

核对日期：2026-09-08。**这里的条目数不是独立数据集数量；同来源的语料、问题、标注、实验复用分别登记。**

[返回入口](README.md) · [完整扫描分组](ALL_LOCATIONS.md) · 机器清单（本地材料：`assets.json`） · 来源关系证据（本地材料：`../docs/data/dataset-ledger-2026-09-08/relationships.json`）

## 商品语料

| 材料与入口 | 数量单位 | 性质与关系 |
| --- | --- | --- |
| **KuaiSearch 当前商品主库**<br>[文件1](F:/agent/datasets/current/kuaisearch/documents.jsonl) / [文件2](F:/agent/datasets/current/kuaisearch/queries.jsonl) | 46,079 商品；507 Query | 公开商品/Query；439 手机 manifest 绑定此 documents 的 SHA256。当前底座 |
| **439 二手手机**<br>[文件1](F:/agent/datasets/current/used-phone/catalog.jsonl) / [文件2](F:/agent/datasets/current/used-phone/manifest.json) / [文件3](F:/agent/datasets/current/used-phone/prices.jsonl) | 439 商品 | 筛选派生；价格为合成参考价；KuaiSearch 主库商品子集。当前底座 |
| **KuaiSearch Lite 行为原件**<br>[文件1](D:/agent-datasets/kuaisearch-lite-09807c773ce67360ed8df30842e372182fcf7ad9/items_lite.train.jsonl) / [文件2](D:/agent-datasets/kuaisearch-lite-09807c773ce67360ed8df30842e372182fcf7ad9/recall_lite.train.jsonl) | items 2,789,868,956 字节；recall 257,794,992 字节 | 公开 Lite 行为数据；本次大文件只核位置/大小；V14 行为记忆来源；不能直接并入 relevance 商品 ID 空间。来源保留 |
| **Shopping Companion 原始数据**<br>[文件1](F:/agent/data/raw/shopping-companion/9a8a2a1c13f0d88de070238352bcf71f98ca851f/train.parquet) / [文件2](F:/agent/data/raw/shopping-companion/9a8a2a1c13f0d88de070238352bcf71f98ca851f/test.parquet) | train/test Parquet；行数本次未重算 | 独立公开来源；V13 记忆包声明的原始来源；不同于 KuaiSearch。历史来源 |

## 单轮检索

| 材料与入口 | 数量单位 | 性质与关系 |
| --- | --- | --- |
| **手机 24 Query 与最终标注**<br>[文件1](F:/agent/datasets/benchmarks/phone-retrieval/scenarios/selected_scenarios_v1.jsonl) / [文件2](F:/agent/datasets/benchmarks/phone-retrieval/review-and-score/final_qrels/final_qrels.jsonl) · [结果](F:/agent/datasets/benchmarks/phone-retrieval/review-and-score/score_report.json) | 24 主检索；另3边界；2,175 标注对 | 真实 Query 来源；Codex 辅助标注，非人工金标；development12/validation8/原测试4；3条池内无相关商品不纳入相关性评分。当前检索基准 |
| **多品类 Query 与来源标签**<br>[文件1](F:/agent/datasets/support/multicategory-breadth/queries.jsonl) / [文件2](F:/agent/datasets/support/multicategory-breadth/source_qrels.jsonl) / [文件3](F:/agent/datasets/support/multicategory-breadth/human_sample_labels.jsonl) | 507 Query；90 行抽样审查标签 | 来源标签/部分审查；与主库507按来源对接；不是507完整人工金标。配套证据 |
| **多品类阶段仲裁**<br>[文件1](F:/agent/datasets/support/multicategory-adjudication/adjudicated_qrels.jsonl) / [文件2](F:/agent/datasets/support/multicategory-adjudication/manifest.json) | 12 Query、124 对（33 UNKNOWN） | 阶段性仲裁；仅第二审阅测试通道，不覆盖全部507。阶段证据 |

## 多轮 Agent

| 材料与入口 | 数量单位 | 性质与关系 |
| --- | --- | --- |
| **Agent 行为场景**<br>[文件1](F:/agent/agent/evaluation/assets/used_phone_harness_behavior_v1_20260825/public/scenarios.jsonl) / [文件2](F:/agent/agent/evaluation/assets/used_phone_harness_behavior_v1_20260825/manifest.json) | 24 场景 / 65 轮 | 8 real_web＋8 real_web_derived＋8 architecture_authored；TaskState/Context AB v6 直接复用；与真人回放有重叠。保留证据/当前入口 |
| **TaskState/Context A/B v6**<br>[文件1](F:/agent/agent/evaluation/assets/shopping_task_state_context_ab_v6_20260829/selection.json) / [文件2](F:/agent/agent/evaluation/assets/shopping_task_state_context_ab_v6_20260829/manifest.json) | 复用同一24场景65轮，新增独立场景0 | 实验选择合同；sourceDataset 指向 harness24；不能再次累计场景数。保留证据/当前入口 |
| **真人会话回放 V7**<br>[文件1](F:/agent/agent/evaluation/real_user_multiturn_replay_20260903_v7/conversations.jsonl) · [结果](F:/agent/agent/evaluation/real_user_multiturn_replay_20260903_v7/FINAL_EVIDENCE_AND_DECISION_2026-09-03.md) | 8 会话 / 21 轮 | 项目主人确认的真实网页对话；逐字核对6会话/16轮与harness24一致；另外2会话不能直接称相同子集。保留证据/当前入口 |
| **手机多轮来源收集**<br>[文件1](F:/agent/datasets/benchmarks/phone-retrieval/scenarios/selected_multiturn_scenarios_v1.jsonl) | 3 条多轮场景记录 | 真人来源登记；与真人回放共享部分来源，不是额外3个已独立验收benchmark。来源材料 |
| **ReAct 架构24 v4**<br>[文件1](F:/agent/agent/evaluation/assets/react_v1_architecture_24_v4/public/scenarios.jsonl) / [文件2](F:/agent/agent/evaluation/assets/react_v1_architecture_24_v4/provenance_receipt.json) | 24 场景 / 33 轮 | 架构设计脚本；与24条主检索、24场景65轮行为集均不同；输入包状态不代替实验结果。保留证据/当前入口 |
| **ReAct 泛化场景**<br>[文件1](F:/agent/agent/evaluation/assets/used_phone_react_generalization_v1_20260826/public/scenarios.jsonl) | 9 场景 / 21 轮 | architecture_authored_heldout；独立用途的留出脚本；不当作真人对话。保留证据/当前入口 |
| **购物任务 Mission MVP**<br>[文件1](F:/agent/agent/evaluation/assets/shopping_mission_benchmark_v1_mvp_20260823/public/scenarios.jsonl) / [文件2](F:/agent/agent/evaluation/assets/shopping_mission_benchmark_v1_mvp_20260823/manifest.json) | 18 场景 / 53 轮 | 任务脚本，具体来源看manifest；有独立private任务oracle；不与单轮qrel合并。历史能力基准 |
| **TaskState MVP r2**<br>[文件1](F:/agent/agent/evaluation/assets/shopping_task_state_v2_mvp_r2_20260822/public/scenarios.jsonl) | 14 场景 / 56 轮 | 状态转换脚本；有private状态oracle；版本不当成新真人样本。历史能力基准 |
| **Context/Multi-Agent 公开pilot**<br>[文件1](F:/agent/agent/evaluation/assets/context_multiagent_public_pilot_v1_20260831/scenarios.jsonl) | 33 场景 / 86 个turn字段记录 | 公开开发场景；具体来源逐条保留；turn字段含历史上下文，不等于86次独立在线用户请求。保留证据/当前入口 |
| **通用长对话**<br>[文件1](F:/agent/agent/evaluation/context_history_strategies_v1_20260905/core_dataset72_003/script.json) | 1 条脚本 / 72 轮 | 真实KuaiSearch Query播种＋AI合成续问；同family修订/扩写不独立累计；详见来源关系表。长对话输入 |
| **vivo长对话**<br>[文件1](F:/agent/agent/evaluation/context_history_strategies_v1_20260905/core_dataset48_vivo002/script.json) | 1 条脚本 / 48 轮 | 真实KuaiSearch Query播种＋AI合成续问；同family修订/扩写不独立累计；详见来源关系表。长对话输入 |
| **苹果长对话**<br>[文件1](F:/agent/agent/evaluation/context_history_strategies_v1_20260905/core_dataset24_iphone001/script.json) | 1 条脚本 / 24 轮 | 真实KuaiSearch Query播种＋AI合成续问；同family修订/扩写不独立累计；详见来源关系表。长对话输入 |
| **Context 首版完整 A/B/C**<br>[文件1](E:/context-c-release002-20260906/agent/evaluation/context_history_strategies_v1_20260905/core_dataset48_vivo002/script.json) · [结果](E:/context-c-release002-20260906/FIRST_EDITION_ABC_REPORT.md) | 同一48轮开发脚本，三个实验臂 | 开发实验；不是144条独立问题；与F盘vivo48脚本SHA256完全相同；C完成但延迟门失败。已有实验结果 |
| **合成长历史对照**<br>[文件1](F:/agent/agent/evaluation/context_raw_full_vs_compiled_v1_20260902_v1/scenarios.jsonl) | 25 条场景记录 | 合成长历史压力测试；不是25条真人会话，也不是检索24扩样。保留证据/当前入口 |
| **Multi-Agent 条件式确认**<br>[文件1](F:/agent/agent/evaluation/assets/multi_agent_runtime_v2_confirmation_20260902/scenarios.jsonl) / [文件2](F:/agent/agent/evaluation/multi_agent_runtime_v2_confirmation_20260902_v1/preregistered-manifest.json) | 8 条合成场景 | 冻结合成留出；manifest哈希绑定439商品目录。保留证据/当前入口 |
| **早期自然导购 guide15**<br>[文件1](F:/agent/.agents/evaluation-assets/used-phone-natural-guide-dev15-20260814/cases_public.jsonl) | 15 场景 / 18 轮 | 开发/验证脚本；保留早期覆盖；不自动加入当前主检索或真人池。历史能力基准 |
| **早期自然导购 guide20**<br>[文件1](F:/agent/.agents/evaluation-assets/used-phone-natural-guide-validation20-v2-20260814/cases_public.jsonl) | 20 场景 / 25 轮 | 开发/验证脚本；保留早期覆盖；不自动加入当前主检索或真人池。历史能力基准 |

## 跨会话记忆

| 材料与入口 | 数量单位 | 性质与关系 |
| --- | --- | --- |
| **自然候选记忆首版**<br>[文件1](D:/agent-experiments/memory-natural-v1-20260907/frozen002/agent/evaluation/memory_natural_v1_20260907/dataset.py) / [文件2](D:/agent-experiments/memory-natural-v1-20260907/frozen002/agent/evaluation/memory_natural_v1_20260907/trajectories.py) · [结果](D:/agent-experiments/memory-natural-v1-20260907/FIRST_EDITION_REPORT.md) | 24开发＋24验收案例；6条三会话验收轨迹 | 合成候选/轨迹；M0/M1/M2重复实验臂；输入定义在冻结Python文件中，不是缺数据；规模来自原报告，未执行生成器。当前有界实验 |
| **Shopping Companion 记忆问答 V13**<br>[文件1](F:/agent/agent/evaluation/assets/shopping_memory_v13_20260830/dev.jsonl) / [文件2](F:/agent/agent/evaluation/assets/shopping_memory_v13_20260830/validation.jsonl) / [文件3](F:/agent/agent/evaluation/assets/shopping_memory_v13_20260830/sealed-test.jsonl) / [文件4](F:/agent/agent/evaluation/assets/shopping_memory_v13_20260830/manifest.json) | 600开发＋200验证＋200原测试问题 | 公开来源派生问答，含memoryEpisodes字段；问题条数不等于独立真人会话数；来源不是KuaiSearch。历史独立来源 |
| **KuaiSearch Lite 行为记忆**<br>[文件1](F:/agent/agent/evaluation/assets/kuaisearch_lite_phone_behavior_memory_v2_2_public_20260830/manifest.json) | manifest声明严格手机items 27,190；本次未重算 | 用户行为关联，非真人多轮聊天；绑定D盘Lite原件；不得混入439手机商品数。行为来源/历史评测 |
| **记忆治理 ABC 场景**<br>[文件1](F:/agent/agent/evaluation/assets/shopping_memory_abc_v1_20260829/scenarios.jsonl) | 24 条case | 记忆记录应用脚本；不是24条主检索，也不是24个自然用户。历史能力基准 |

## 外部/历史基准

| 材料与入口 | 数量单位 | 性质与关系 |
| --- | --- | --- |
| **Amazon ESCI**<br>[文件1](F:/agent/agent/evaluation/external_public_esci_task1_20260902_attempt001/RESULT.md) · [结果](F:/agent/agent/evaluation/external_public_esci_task1_20260902_attempt001/RESULT.md) | 50 Query / 1,036 标注pair / 1,035 商品（报告口径） | 独立英文公开人工标注基准；退出当前KuaiSearch主线；不是全库召回评测。外部证据 |
| **Yelp RAG**<br>[文件1](F:/agent/agent/knowledge_data/eval/q15_sealed_test_report.json) | 6 道sealed题（已有报告口径） | 商家评论RAG，非电商商品检索；暂退出当前电商主线；其余评论/推荐资产见完整盘点。历史证据 |

## 商品知识开发验证

开发题原文（本地材料：`../docs/acceptance/product-knowledge-mcp-20260908/development-cases-v1.jsonl`）：4会话8轮，Codex编写；使用同一439商品和phone-v2知识。反复调试可见，不是真人21轮子集或未见benchmark。对照结果见[最终记录](../docs/acceptance/product-knowledge-mcp-20260908/RESULT.md)。

## 不要重复计数

- 手机24条主检索、ReAct架构24场景33轮、Agent行为24场景65轮是不同材料。
- 真人回放8会话中，有6会话共16轮与行为集逐字一致；另2会话没有完整逐字匹配，不强行认定同一子集。
- Context状态A/B v6复用了同一行为集，没有新增24个独立场景。
- F盘vivo48与E盘ABC实验脚本字节一致；三个实验臂不产生三倍独立样本。
- Context旧版、扩写版按familyId登记；全部原文见关系清单。
- 大型下载、SQLite和运行结果本次按位置/元数据登记；未重跑模型、未生成新标注，未重新确认历史效果。
