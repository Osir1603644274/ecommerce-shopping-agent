# Multi-Agent V2 公开开发配对：最终证据与结论

## 结论

`BOUNDED_MULTI_AGENT_V2_DEVELOPMENT_ACCEPT`。

旧 V1 在 9 个 research 场景中单 Agent 与 Multi-Agent 均为 `4/9`。本轮定位到子 Agent 压缩报告丢失“机况选择”信息，新增绑定 `taskId/revision/CandidateScope/agentRunId/sequence` 的请求—回执，并将服务端核验的最小决策摘要回传父 Agent。

## 数据

- 全部 33 场景：强单 Agent `29/33`，MA2 `31/33`。
- 9 个 research 场景：强单 Agent `5/9`，MA2 `7/9`；MA2 独有成功 2，反向 0。
- Claim precision：两臂均 `0.7778`。
- 父 Agent 最终上下文估算 Token：P50 `2588 → 2033`，均值降低 `23.17%`。
- 总 Provider Input Token：`54838 → 54510`，基本持平。
- P95 延迟：`9612.815 → 9904.398 ms`；安全失败 0，通信合同失败 0。

## 失败保全

原 attempt 在 66 条 arm trace 与 36 条模型回执全部落盘后，因汇总器误读 SLO 字段名退出。失败目录保留；相邻 remediation 只执行零模型调用重算，未覆盖 trace 或改变阈值。

## 边界

这是公开开发集 remediation，不是 untouched confirmation；McNemar 双侧 `p=0.5`，Bootstrap 质量差异区间仍含 0。允许写“公开开发集从 5/9 提升到 7/9”，禁止写“已证明 Multi-Agent 普遍提升质量”或“生产默认已切换”。
