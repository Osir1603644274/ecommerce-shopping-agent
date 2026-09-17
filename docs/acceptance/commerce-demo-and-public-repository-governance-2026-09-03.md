# 电商全链路 Demo 与公开仓库治理自审

- 日期：2026-09-03
- 决策：`BOUNDED_LOCAL_DEMO_AND_PUBLIC_SNAPSHOT_ACCEPT`
- 生产默认：`HOLD`
- 范围：本机单实例、模拟支付回调、公开候选快照；不含远程发布、真实支付、多机容灾或跨系统 Exactly-once。

## 1. 真实交易链

`scripts/commerce-demo.ps1 smoke` 唯一成功链路：

| 阶段 | 结果 |
|---|---|
| Agent 商品检索 | requestId `req-789d65806fd4`，商品 `17832` |
| 服务端候选校验与订单预览 | CandidateScope、JWT、Java 预览、Redis 提案均通过 |
| 精确确认下单 | requestId `req-8bfa89a67a01`，工具 `transaction_handoff/create_order` |
| 精确确认支付 | requestId `req-7858a6247d36`，工具 `transaction_handoff/create_payment` |
| 本地模拟回调 | paymentId `5b9bc5db-1d61-496a-ad9f-6f9e8a8a27f2`，状态 `SUCCESS` |
| Agent 权威回查 | requestId `req-408bac3095dd`，工具 `query_order_status`，读取 `PAID` |

MySQL 权威记录：

- orderId：`35a582f6-8d20-4060-8f4a-04f7a2e21ffd`
- orderNo：`O202609030611450231CB0F7C71`
- 订单状态：`PAID`，version `1`
- 支付状态：`SUCCESS`，version `1`
- 金额：`38796` 分

Kafka 消费日志记录 `order.created.v1` 与 `order.paid.v1` 均被可靠消费。该事实只证明本次本地链路完成。

## 2. 安全与权限自审

- 普通配置中的 `AGENT_TRANSACTION_ENABLED`、`COMMERCE_DEMO_ENABLED`、`COMMERCE_DEMO_PAYMENT_SIMULATION_ENABLED`、`PAYMENT_SIMULATOR_ENABLED` 均为 `false`。
- Demo 启动脚本仅在本地进程中显式开启；支付模拟必须同时通过 Agent 和 Java 两个开关。
- Java access/refresh token 只写 Redis，浏览器只得到 HttpOnly Cookie 与 CSRF token。
- 所有可写浏览器请求要求同源与 CSRF；`/me` 的 CSRF 轮换也拒绝 cross-site 请求。
- 订单预览只接受当前 TaskState/CandidateScope 中真实展示的商品。
- 预览不写交易事实；下一条消息必须精确为 `确认下单` 或 `确认发起支付`。
- Java 后端再次验证用户身份、订单归属、库存、状态和幂等键；Agent 不直接写 MySQL。

## 3. 页面与架构自审

- 桌面端以 1440×1100 截图检查：登录、导购、交易、当前架构和服务端回执分区无重叠。
- 移动端以 390×1200 截图检查并修复顶部导航挤压；内容改为纵向排列。
- `/agent-flow` 同时区分真实只读 DebugTurn、生产 `react_v1` 参考链与全系统边界。
- Java 事务只展示完成回执，未伪装成可逐语句暂停的 Agent 节点。

## 4. 测试与构建

- Agent 定向回归：`72 passed`。
- 公开候选快照内重复执行同一定向回归：`72 passed`。
- Demo JavaScript：`node --check` 通过。
- Python 新增模块：`py_compile` 通过。
- Java 17 Docker 构建：backend 测试门缓存命中且镜像构建通过；gateway 测试门重新执行并构建通过。
- 本机 Maven 使用 Java 11，因 class version 61/55 不兼容而失败；这是本机工具链不满足项目 Java 17 合同，不计为源码测试失败。

## 5. 公开仓库治理

- 根 README 只呈现当前产品主线、30 分钟启动、目录边界和诚实边界。
- `PUBLIC_REPOSITORY_GUIDE.md` 明确 Agent、Java、evaluation、legacy 与 Spring Cloud 非默认边界。
- `FEATURE_EVIDENCE_INDEX.md` 建立功能→源码→测试→证据映射。
- `build-public-snapshot.ps1` 采用允许列表，只复制运行源码、必要测试和公开文档。
- `check_public_snapshot.py` 独立复算文件集合、大小、常见密钥模式和 SHA-256 manifest。
- 最终公开候选快照：`.runtime/public-snapshot-v9`，manifest 固定 `1028` 个文件；独立复核为密钥命中 `0`、超限文件 `0`、manifest 错配 `0`，快照内 Markdown 链接与仓库卫生检查均通过；测试产生的未声明 bytecode/pytest 缓存不属于公开资产。

## 6. 允许与禁止表述

允许：

> 打通电商导购 Agent 到 Java 交易后端的本地完整 Demo，经候选范围、JWT 与双确认后完成下单、支付模拟回调，并由 Agent 回查 MySQL 权威订单状态。

禁止：

- “已接入真实支付”；
- “已实现跨系统 Exactly-once”；
- “已完成多机/生产级验收”；
- “已发布到 GitHub”；
- 将本次 Demo 外推为 Context、记忆或 Multi-Agent 的质量结论。
