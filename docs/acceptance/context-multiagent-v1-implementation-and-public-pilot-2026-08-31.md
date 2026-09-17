# Context + Multi-Agent V1 实现与 Public Pilot 验收

- 日期：2026-08-31
- 计划状态：`PLAN_ACCEPT_WITH_SCOPE_REDUCTION`
- 架构合同：`CONTRACT_ACCEPT`
- 自动配对结论：`ENGINEERING_HOLD`
- 人工节点：`HOLD_PENDING_THIRD_BLIND_ADJUDICATION`
- 生产默认：`HOLD_KEEP_CURRENT_DEFAULT`

## 1. 已冻结并实现的边界

```text
RetrievalService
→ server-owned CandidateScope
→ deterministic InvestigationSet (unique ranked subset, max 8)
→ EvidenceResearchAgent (read only, exact scope)
→ bound ResearchReport
→ parent MergeValidator / Validator
→ ShoppingAgent final answer
```

EvidenceResearchAgent 不发现新商品，不扩大 CandidateScope，不写 TaskState、Memory、
Scope 或交易，不发布最终推荐，不使用交易工具。TransactionWorkflow 是无模型确定性
后端路径，不计入 Multi-Agent 数量或效果。

核心冻结文件：

- `docs/adr/context-multiagent-v1.md`
- `docs/adr/context-multiagent-v1-authority-matrix.md`
- `schemas/context-multiagent-v1/`
- `evaluation/data-lineage-v1.json`
- `evaluation/preregistration-context-multiagent-v1.md`
- `evaluation/contract-freeze-manifest-v3.json`

## 2. 确定性合同与 Context 迁移

- 合同 active attempt：`context_multiagent_contract_v2_attempt002`
- 合同结果：69/69，`CONTRACT_ACCEPT`
- CTX0→CTX1a：65/65 行为等价
- 受保护字段：65/65
- 15 个指代 query：历史全部保留
- 50 个非指代 case：Context 全部缩减
- 估算 token P50：CTX1a 412，CTX1b 307
- Context 结论：`BOUNDED_CONTEXT_ACCEPT`，不等于效果或生产切换授权

## 3. Provider 兼容失败与版本化恢复

V1 attempt001 原样保留。18 个研究臂均被 DeepSeek provider 拒绝：

```text
BadRequestError: Thinking mode does not support this tool_choice
```

该尝试不能用于 CTX1b/MA1 质量比较。Amendment 003 只在带 tools 的请求中设置
`thinking.type=disabled`，保留模型、temperature、Prompt、数据、预算、deadline、seed、
scorer、分母和 gate。非计分 smoke 真实通过：

- forced tool choice：保留
- max retries：0
- receipt hash：`e5b75027afed6700d77fe4b8ee233643f9f1d67340307ad95271c513743a0f61`

## 4. Public paired attempt002

- manifest SHA256：`07eae044f7fa14fdb57bd71f362e999bacbe4153013df1cd2b30d20d7f0da626`
- source clusters：33
- research pairs：9
- provider calls：36/36 succeeded
- retry ordinal：全部 0
- token status：全部 `OBSERVED`
- arm execution failures：0
- contract safety failures：0
- raw child observation leaks：0
- shared pair identity：9/9 exact

| 指标 | CTX1b | MA1 |
|---|---:|---:|
| 全任务成功 | 28/33 | 28/33 |
| Research success | 4/9 | 4/9 |
| Evidence claim precision macro | 0.7778 | 0.7778 |
| Parent final estimated tokens P50 | 2577 | 1209 |
| Parent final estimated tokens P95 | 3166 | 1462.6 |
| Total provider input tokens | 54,601 | 42,100 |
| Scenario latency P95 ms | 10,262.96 | 10,203.77 |

MA1 父上下文降低 53.32%，SLO 通过；任务成功完全持平，McNemar discordant=0、
p=1.0。预注册要求 MA1 至少多解决 2 个复杂场景，实际为 0，因此必须保留
`ENGINEERING_HOLD`。父上下文缩减不能被扩大解释为 Multi-Agent 质量更优。

证据：

- result hash：`bab12a15d31adfbaafdc0f8428ca4d56c9214102716e2cf441c96c0a04533f0e`
- traces SHA256：`71c28a6938a105d322baa35d9f4afeae03b0cfe2fab24d0283fc44a700730fbd`
- model receipts SHA256：`e30fc7bd3e81a4df8b03f436967fea79ce6b5ee1c249a9253dffd8bab52a3e08`
- artifact-chain validator：33/33，report hash
  `b8e88416d7873051178be08dd06207089ddcaa22728605da121a62ae95366540`

## 5. 人工盲审节点

9 个研究场景产生用户可见文本差异。已按固定 seed 生成两份逐项完全镜像的匿名包：

- `blind-review-v1/reviewer01.jsonl`
- `blind-review-v1/reviewer02.jsonl`
- public 包身份泄漏检查：0
- package receipt hash：`47f2b29985ee14766762e1158966391993518a497d28c9ee6145cc5829ed582d`

两名不同人工已完成 9/9 独立评分，并在解盲前确认未查看 sealed mapping、另一人的
评分或自动实验结果。哈希绑定后机械解盲结果为：18 个 reviewer-item 判断中
`CTX1b=16`、`MA1=1`、`tie=1`；7/9 项两人一致，且一致项全部偏好 CTX1b。
分歧仅为 `blind-01`（CTX1b/MA1 各一票）和 `blind-06`（CTX1b/tie）。

两名评审的四维均值为：

| 维度 | CTX1b | MA1 |
|---|---:|---:|
| constraintFidelity | 4.7222 | 3.5556 |
| evidenceDiscipline | 4.3889 | 3.0000 |
| taskProgression | 4.8889 | 3.8333 |
| usefulness | 4.7778 | 3.2222 |

该结果只说明本批用户可见回答明显偏向 CTX1b；它不能覆盖自动
`ENGINEERING_HOLD`。解盲结果 hash 为
`439159428a1a5e7525d7b6be5a642274183e7dc8c894e5363dda05fe00f875a5`。
两项分歧已另行生成无身份泄漏的第三人盲裁决包，生产默认仍不变。

## 6. TransactionWorkflow 独立回归

按冻结计划保留现有 `transaction_agent` 代码路径名，架构和报告统一称
`TransactionWorkflow`；不进行无价值的大规模文件重命名。

当前路径在模型调用前确定性处理精确确认，模型工具菜单不暴露交易写工具；身份、
CandidateScope/revision、一次性 Redis 确认和 Java `Idempotency-Key` 均由服务端处理。
本轮独立回归：28/28 通过，覆盖交易上下文伪造拒绝、capability、一次性确认、
后端幂等合同与 UNKNOWN/reconcile 状态机。它不计入 Multi-Agent 成绩。

## 7. 测试与默认状态

- Context/Multi-Agent 相关定向回归：76/76
- TransactionWorkflow 独立回归：28/28
- 先前同一实现批次广泛 Python 回归：336 passed
- 所有新增生产开关保持 default-off
- 未修改 fixed/ReAct、TaskState、Memory 或生产 Multi-Agent 默认
- 实验所启动的 `local-life-redis` 已恢复到原先 stopped 状态
- 未执行 reset、clean、stash、checkout、rebase、commit 或 push

## 8. 当前唯一人工输入

将以下独立目录中的 `adjudicator.jsonl` 交给第三名、且不同于前两名的人工：

`agent/evaluation/distributions/context_multiagent_public_pilot_v2_attempt002_adjudicator_v1_attempt002/`

该目录仅含两项分歧、说明、模板和公开 receipt；身份泄漏检查为 0，packet SHA256 为
`ed9970bb9771654b96508a2b7256885896b7e561d38526484b90df2a06e1ecbc`。
第三人评分前不得查看 sealed mapping、前两人评分、解盲结果、自动结果或 trace。
其偏好用于裁决对应分歧项；若选择 tie，则该项最终记 tie。人工裁决仍不能修改自动
gate 或切换生产默认。
