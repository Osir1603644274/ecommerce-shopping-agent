# Context + Multi-Agent V1 Schemas

这些 Schema 是 V1 冻结合同，不代表实现已经完成。所有 JSON 使用 Draft 2020-12。

| Schema | 用途 |
|---|---|
| `run-context.schema.json` | 服务端运行、主体、阶段和 capability 绑定 |
| `context-item.schema.json` | 带权威、作用域和生命周期的上下文项 |
| `context-receipt.schema.json` | 编译选择/拒绝原因与双哈希回执 |
| `investigation-set.schema.json` | 当前 CandidateScope 内最多 8 个调查候选 |
| `research-request.schema.json` | 父侧签发的有界研究请求 |
| `research-report.schema.json` | 子 Agent 的结构化证据报告 |
| `route-decision.schema.json` | 五类路由结果 |
| `model-call-receipt.schema.json` | 逐 provider 调用归因与 usage |

权限不由这些模型可见对象授予。真实工具能力必须来自服务端 capability grant。
