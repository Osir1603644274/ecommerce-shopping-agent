# Phase 1B 真实证据源、身份绑定与 Agent 资格审计报告 V2

## 结论

```text
GATE_1_REJECT / EXPANDED_PUBLIC_SOURCE_SET_V2
EvidenceResearchAgent V2: HOLD_DO_NOT_IMPLEMENT
Production default: KEEP_SINGLE_AGENT
ContextCompiler: UNCHANGED_BY_THIS_AUDIT
```

该结论只针对本次冻结的 439 条二手手机数据、24 个公开来源试点簇和已核验来源。它不证明 Multi-Agent 一般无效，也不推翻此前 ContextCompiler 的有界结论。

## 本次真正执行了什么

1. 冻结 439 条目录，逐条做保守标题型号声明解析。
2. 将每条结果分为 `SINGLE / AMBIGUOUS / UNRESOLVED`，不把结构化属性反用作型号真值。
3. 在查询外部来源前，按预注册的品牌配额、频次和词典序规则冻结 24 个型号簇。
4. 对每个簇分别核验官方型号入口、DXOMARK、Notebookcheck 和来源条款。
5. 区分独立发布者自己的受控测试、仅链接外部文章的资料库页、新闻页和未找到。
6. 对四类证据缺口执行工作流可替代性审计，并按硬门给出 Gate 1 结论。

外部 GPT 报告只作为发现线索。其会话内引用编号没有被当作证据；最终产物只保留重新核验的公开 URL、发布者、来源能力边界和许可状态。

## 定量结果

| 项目 | 结果 |
|---|---:|
| 冻结目录 | 439 条 |
| 单一标题型号声明 | 320 / 439（72.89%） |
| 多型号或歧义标题 | 79 / 439（18.00%） |
| 未解析标题 | 40 / 439（9.11%） |
| 冻结公开试点 | 24 个型号簇、92 条商品 |
| 官方精确型号入口 | 22 / 24（91.67%） |
| 至少一个独立受控性能测试 | 11 / 24（45.83%） |
| 可信物理商品身份绑定 | 0 / 24 |
| observation-dependent 调查轨迹 | 0 |
| Agent-eligible gap family | 0 |

这里的 `22 / 24` 只说明官方页面能对应卖家标题声称的型号，不说明这台二手商品确实是该型号，更不说明电池、屏幕或维修状态真实。

## 为什么公开来源仍不能形成 ResearchAgent V2

### 1. 官方页面解决的是型号级事实

官方规格或支持页可证明某个型号的标准配置、官方命名或适用范围。其正确实现是确定性的 `EvidenceService`：

```text
保守型号声明
→ 精确型号入口查询
→ 返回型号级事实或 UNKNOWN
```

该过程不需要子 Agent 动态规划。

### 2. 独立测试仍是固定来源工作流

DXOMARK 和 Notebookcheck 对部分型号提供独立测试，但冻结来源下的流程仍可完全枚举：

```text
型号声明
→ 查 DXOMARK
→ 查 Notebookcheck
→ 区分 own test / library / news
→ 返回模型级证据或 UNKNOWN
```

这应实现为固定 `EvidenceWorkflow`。一次 observation 不会语义性地改变下一工具、调查顺序或停止策略。

### 3. 二手商品关键事实缺少身份连接

公开型号页无法回答以下商品级问题：

- 当前这台机器的电池健康度；
- 是否维修或进水；
- 屏幕、电池等部件是否原装；
- 当前设备的真实性能衰减；
- 标题里的型号是否就是物理设备身份。

本地数据没有可供公开来源回查的 IMEI、序列号、平台验机报告 ID 或等价可信 join key。因此正确行为是 `UNKNOWN`，而不是让 Agent 搜索更多网页后猜测。

### 4. 许可不支持把网页内容冻结成公开评测库

本次核验的官方站点和独立测试发布者均未提供本评测所需的批量抓取与快照再分发授权。产物只保存 URL 与人工核验摘要，没有复制网页正文。若未来要做稳定生产适配器或可再分发数据集，需要官方 API、书面许可或许可证明确的数据源。

## 工作流可替代性判定

| gap family | 正确实现 | Agent eligible |
|---|---|---:|
| 标题型号声明 → 官方规格 | `EvidenceService` | 否 |
| 型号 → 独立性能测试 | 固定 `EvidenceWorkflow` | 否 |
| 具体二手机况 | 无可信绑定时 `UNKNOWN` | 否 |
| 标题多型号或冲突 | 确定性防线或澄清 | 否 |

24 个簇满足 public pilot 数量门，独立性能证据具备改变模型级推荐理由的潜力；但本轮没有合法可冻结的真实适配器任务轨迹，因此没有把这种潜力记作已观察到的 `EvidenceDelta`。以下硬门失败：

- 至少两类真实 Agent-eligible gap family；
- 每类至少八条独立证据 lineage；
- 后续行动真实依赖前一次 observation；
- 真实任务轨迹中观察到决策相关 `EvidenceDelta`；
- 可信的物理商品身份绑定；
- 可冻结并再分发的来源许可。

因此不能进入 Evidence Contract V2 冻结、ResearchAgent V2 实现或 R0/R1/R2 效果实验。

## 可以继续建设的部分

本次结果支持一个更小、可交付的证据层：

```text
TitleClaimNormalizer
→ OfficialModelEvidenceService
→ IndependentPerformanceEvidenceWorkflow
→ Authority/License Guard
→ UNKNOWN on listing-specific facts
```

它可用于改善单 Agent 的证据纪律，但不得包装成 Multi-Agent 效果提升。

## 何时可以重新打开 Gate 1

只有新增来源同时满足下列条件，才应新建 V3，而不是改写本次结果：

1. 可合法访问并冻结证据或获得生产调用授权；
2. 有可信标识把证据绑定到具体商品或设备；
3. 至少两类任务的下一动作真实依赖前一次 observation；
4. 该动态路径不能由有限状态机、固定 fallback 或一次抽取完整替代；
5. 结果能改变候选资格、排序或关键推荐理由；
6. 有可冻结 gold、预算、停止规则和至少八条独立 lineage/类。

候选来源可以是平台验机报告 API、授权维修/质检链路、可验证的设备序列绑定或具备许可的多来源商品证据服务。仅增加更多型号网页或更多评测文章不会通过 Gate 1。

## 产物与复现

- `phase1b-preregistration.md`：执行前冻结的问题、样本选择与硬门。
- `build_title_model_claim_pilot_v2.py`：439 标题解析和 24 簇冻结。
- `title-model-claims-v2.jsonl`：逐商品解析轨迹与行级哈希。
- `public-pilot-clusters-v2.json`：查询外部来源前冻结的公开试点。
- `source-terms-v2.json`：来源条款和许可边界。
- `source-coverage-pilot-v2.jsonl`：24 簇逐项来源覆盖与权威边界。
- `workflow-substitutability-review-v2.jsonl`：四类 gap 的替代性审计。
- `phase1b-gate1-decision-v2.json`：机器可读终局。
- `validate_phase1b_v2.py`：哈希、行数、绑定、指标和 Gate 一致性校验。

复现命令：

```powershell
python evaluation/context-multiagent-v2-feasibility-v2/build_title_model_claim_pilot_v2.py --catalog data/derived/ecommerce/used_phone_catalog_expansion_kuaisearch_09807c_20260823_r3/catalog.jsonl --output-dir evaluation/context-multiagent-v2-feasibility-v2
python evaluation/context-multiagent-v2-feasibility-v2/build_phase1b_source_audit_v2.py
python evaluation/context-multiagent-v2-feasibility-v2/build_package_manifest_v2.py
python evaluation/context-multiagent-v2-feasibility-v2/validate_phase1b_v2.py
```

预期终行：

```text
PASS phase1b-v2
```

## 限制

- 标题解析是保守规则辅助结果，不是人工型号 ground truth。
- 24 个簇用于可行性审计，不代表整个电商域。
- URL、网页内容和条款会变化；本报告是 `2026-09-01` 的点时审计。
- 本阶段没有运行 ResearchAgent、没有运行模型配对效果实验，也没有修改任何生产默认。
