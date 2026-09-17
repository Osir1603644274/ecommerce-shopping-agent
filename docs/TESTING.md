# 测试与评测

项目同时覆盖功能回归、故障恢复、检索质量、上下文效率和后端并发。以下结果来自 2026-09-03 前完成的本地测试与固定数据集。

## 主要结果

| 方向 | 方法 | 结果 |
|---|---|---|
| Agent 架构 | 24 个任务场景，每种架构执行 33 轮；5 个差异场景由两名评审分别比较 | ReAct / PAE / 平局为 `7 / 0 / 3` |
| Checkpoint | 进程中断、工具重放、旧 Worker 和状态篡改测试 | 130 项恢复用例与 12 项篡改用例通过，重复/缺失副作用为 0，恢复 P95 约 75 ms |
| 商品检索 | KuaiSearch 46,079 篇商品文档、507 条 Query、12 个品类；二手手机 439 件商品、24 条复杂 Query | 对比 ES、Dense、RRF、Cross-Encoder 和 LLM 重排后，在线方案采用 ES Standard Top50、事实核验与确定性重排 |
| 外部检索数据 | Amazon ESCI 的 1,036 个人工标注 pair | 重排 nDCG@10 为 `0.7069` |
| 商家评论 RAG | Yelp 300 家商户、6,774 条评论，Vector+BM25 双路召回 | 6 道独立测试题 nDCG@5 从 `0.368` 提升至 `0.788` |
| 上下文 | 25 组长历史对比和 8 个多轮会话 | 25/25 任务正确，总 Token 减少 `22.9%`、P95 降低 `9.1%`；多轮会话 21/21 结果一致 |
| Multi-Agent | EvidenceResearchAgent 开发集与独立合成测试集 | research 场景从 5/9 迭代至 9/9，独立合成集 8/8，P95 为 5.50 s |
| 库存并发 | 200 个并发请求争抢 50 件库存 | 50 成功、150 售罄、0 超卖 |
| 接口限流 | 100 个并发请求，额度 20 | 放行 20、拒绝 80 |
| 架构对照 | 模块化单体与 Spring Cloud 拆分版使用同源场景 | P50 为 8.08 ms / 16.53 ms，启动为 17.43 s / 45.07 s，当前采用模块化单体 |

## 本地回归

```powershell
python -m pytest -q agent/tests/test_commerce_demo_api.py agent/tests/test_transaction_agent_api.py agent/tests/test_transaction_agent_runtime.py agent/tests/test_chat_endpoint.py
python scripts/check_repository_hygiene.py
python scripts/check_markdown_links.py
```

完整 Demo 的 `smoke` 命令会创建临时用户，依次执行商品检索、订单预览、确认下单、支付预览、支付模拟回调和订单状态查询。

延迟结果与本机硬件、Docker 资源和外部模型服务有关；比较不同方案时应保持数据、依赖和运行环境一致。
