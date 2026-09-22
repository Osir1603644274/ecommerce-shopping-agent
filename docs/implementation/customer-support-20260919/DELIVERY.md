# 电商客服模块交付索引

状态：**工程验收通过（独立 AGENT 复核）；不是人工金标，也不等同于生产就绪。**

## 固定版本验收

- 数据：`evaluation/dataset-draft-008/scenarios.jsonl`，240 个场景、10 类、三轮共 720 次观察。
- 原始运行：`evidence/suite-fixed-006/REPORT.json`。
- 独立复核汇总：`evidence/suite-fixed-006-agent-import-002/REPORT.json`。
- 最终门禁：`evidence/suite-fixed-006-agent-import-002/FINAL_ACCEPTANCE.json`。
- 结果：720/720 自动外部断言通过；三轮各 240/240；硬失败 0；每类均通过 80% 门槛。
- 独立 AGENT 逐断言复核：720/720 行，2469/2469 断言支持；关键事实 2292/2292 支持。`humanReview` 仍为 `UNREVIEWED`，不能称人工金标。
- 时延：有效首响应 P95 1218ms；查询 P95 1193ms；预览 P95 1237ms；三项均满足门槛，缺失观测为 0。
- 用量：客服模型 714 次，6 次 usage/cost 未知；评测对照模型 12 次且计量完整；全部执行调用 726 次。未知成本保持未知，不写成零成本。

## 功能与入口

| 要求 | 证据 | 边界 |
|---|---|---|
| 5173 客户入口、政策与商品 RAG | `frontend/`、`agent/app/customer_support/`、suite-fixed-006 | 评测使用合成商品/订单 |
| 订单、支付、物流查询与催办 | suite-fixed-006 的 `order_payment`、`logistics` 类 | 物流推进由独立模拟器/脚本产生 |
| 仅退款、退货退款、同款同规格换货 | suite-fixed-006 的 `refund_only`、`return_refund`、`exchange` 类 | 签收后七天、数量/金额、验收与库存占用均由外部断言核对 |
| 工单、关闭工单边界、客服对话 | suite-fixed-006 的 `tickets` 类与既有浏览器证据 | 客服模型不能制造工单或业务回执 |
| 独立控制台和固定脚本 | `scripts/customer_support/README.md`、`frontend/test-results-support-demo-console-002` | 管理员凭据只留在私有 runtime/进程内 |
| 业务恢复、未知库存、回执延迟 | suite-fixed-006 的 `recovery` 类及各 batch 原始 SQL/回执 | NEEDS_REVIEW 必须保留，不能用“成功”清除异常 |
| 当前入口预检 | `evidence/preflight-final-demo-003/PREFLIGHT.json` | 5173、18000、18080、19093 均可达；Docker `live-001` 元数据当时为 exited，不能据此宣称容器正在运行 |

## 运行与保护

- 客户入口：`http://127.0.0.1:5173`；隔离客服 BFF：18000；Java 权威服务：18080；独立模拟控制台：19093。
- 启停、隔离数据库、脚本驱动物流/验收/渠道事件见 [OPERATIONS.md](OPERATIONS.md)。
- `runtime/`、`private.json`、`test.env`、JWT、Cookie 和密钥不进入交付包；当前证据包仅保存公开证据、哈希和原始断言引用。
- 不要把默认 8000 的可达性外推为隔离客服链路正确；不要把本地合成套件外推为生产 SLA。
- Qwen 后训练未启动，也未购买算力；本交付验证的是当前工程链路和评测基线。

## 复核说明

`suite-fixed-006-agent-import-002` 由用户授权的无建设上下文审核窗口产生，导入器验证了原始回答字段、证据路径和 SHA-256。它是独立 Agent 证据，不替代真实人工复核；若要发布人工金标或对外生产声明，仍需人工逐断言审核和真实环境部署验收。
