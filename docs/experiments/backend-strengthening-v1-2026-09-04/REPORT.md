# 后端强化 V1：订单分页与可靠异步履约

- 日期：2026-09-04；生命周期：`ACTIVE`；整体结论：`IN_PROGRESS / HOLD_REAL_INFRA_AND_PERFORMANCE`。
- 本轮已完成源码接线与定向验证；履约三开关默认关闭，未重启共享服务、未运行数据库迁移。
- 29 项 Java 定向测试、5 项 Python 仓库模拟器测试通过。它们不等于后端全量回归，也不构成 MySQL/Kafka 或生产验收。
- 0903 的 Context 正式模型采样在后台执行，因此真实基础设施、全量回归和容量/延迟实验留待错峰。代码与验证均在独立副本完成，随后按原始文件 SHA256 核对回写。

## 1. 已实现

| 领域 | 行为 | 主要源码 |
|---|---|---|
| 订单查询 | 新增认证游标分页，`created_at DESC,id DESC`，最大 100 条；签名绑定用户/筛选；明细批查 | `ordering/OrderPageService.java`、`OrderMapper.java` |
| 老接口兼容 | 保持完整订单列表响应，明细按 200 单分批查询，消除逐单查询 | `ordering/OrderService.java` |
| 持久化履约 | 开启后新 PRODUCT 订单的履约登记与下单同事务；专用事件消费者将已支付任务置 READY | `fulfillment/FulfillmentLifecycle.java`、`FulfillmentEvents.java` |
| 事件可靠性 | Inbox 和本地业务效果同事务；事务成功后 ACK；重试耗尽先落数据库死信，支持管理员重放并保留原件 | `FulfillmentKafkaConsumer.java`、`FulfillmentKafkaConfiguration.java`、`FulfillmentDeadLetters.java` |
| 外部副作用 | 租约＋fence、有限线程池、稳定请求键与不可变命令；先查询仓库再发送，未知结果退避/人工重试 | `FulfillmentClaims.java`、`FulfillmentWorker.java`、`HttpWarehouseGateway.java` |
| 交易竞态 | 退款与出库统一先锁订单；退款先赢取消任务，出库或未知状态拒绝直接退款；确认收货须已出库 | `FulfillmentLifecycle.java`、`OrderService.java` |
| 可追踪性 | 任务/尝试记录、操作者审计、队列/运行中计数和调用延迟指标 | `FulfillmentStore.java`、`FulfillmentWorker.java` |
| 验证工具 | SQLite 持久仓库模拟器、提交后丢响应故障、MySQL 同页 SQL 对照脚本、k6 分页、独立 Compose | [`验证工具说明`](../../../scripts/backend-strengthening/README.md) |

Java 文件位于 `backend/src/main/java/com/example/locallife/` 下。仅新增迁移 `V15__durable_order_fulfillment.sql`，旧迁移原件未修改。使用既有 `(user_id,created_at)` 索引；是否增加其他索引等待真实 EXPLAIN 与查询分布，未凭经验先加。

## 2. 原始验证

| 测试类/工具 | 通过数 | 覆盖边界 |
|---|---:|---|
| OrderPageIntegrationTests | 6 | 同时间戳稳定排序、新增头部订单后的遍历、游标防篡改/用户筛选隔离、批量查询、老接口、HTTP 权限 |
| FulfillmentIntegrationTests | 8 | 重复/冲突事件、退款先赢、过期租约旧 Worker 拒绝覆盖、未知结果预算、回执绑定、库存不重复扣、对账、死信原件重放 |
| FulfillmentCommitTests | 3 | 无测试外层事务：注入失败后的回滚、真实提交后重放、两线程退款/领取竞态、Worker 外部提交后丢响应恢复 |
| WarehouseHttpTests | 1 | 真实本机 HTTP：仓库记录后返回错误，再按原键查询回执 |
| OrderServiceIntegrationTests | 8 | 既有订单事务与业务合同定向回归 |
| CommerceHttpContractTests | 2 | 既有交易 HTTP 合同 |
| ModularityTests | 1 | Spring Modulith 模块依赖与边界 |
| Python WarehouseTests | 5 | SQLite 重启持久化、并发幂等、键/负载冲突、提交后断开连接、不可用后恢复 |

Java 原始 XML 见 `evidence/surefire`（本地材料：`evidence/surefire`），执行日志为 `backend-focused-verified.log`（本地材料：`evidence/backend-focused-verified.log`），模拟器结果为 `warehouse-tests-002.log`（本地材料：`evidence/warehouse-tests-002.log`）。机器汇总与证据哈希见 `evidence/validation.json`（本地材料：`evidence/validation.json`）、`evidence/manifest.json`（本地材料：`evidence/manifest.json`）。

运行环境：Windows、JDK **21.0.10**、Maven Wrapper 3.9.16，Java 编译目标 17；定向执行限制 `-Xmx512m -XX:ActiveProcessorCount=1`；Python 3.12。Java 数据库验证使用 H2，Kafka 消费配置加载时禁止自动启动，因此未连接 broker。

```powershell
# 在隔离 backend 目录执行；JAVA_HOME 必须指向实际 JDK
.\mvnw.cmd -o -q '-Dtest=FulfillmentIntegrationTests,FulfillmentCommitTests,WarehouseHttpTests,OrderPageIntegrationTests,OrderServiceIntegrationTests,CommerceHttpContractTests,ModularityTests' '-DargLine=-Xmx512m -XX:ActiveProcessorCount=1' test
```

工具静态检查：Python 脚本语法解析通过；独立 Compose `config --quiet` 通过。未启动 Compose 或执行 SQL/k6 对照。

## 3. 尚未完成的证据门

- [ ] JDK 17 下后端全量回归。既有 `201/201` 属此前源码结果，不能直接用于本次新增源码。
- [ ] 真实 MySQL 上 V15 迁移及回滚/锁竞争验证，特别是 InnoDB 与 H2 差异。
- [ ] 真实 Kafka 消费、offset、消费者重启与死信写入失败恢复；真实 Java→HTTP 仓库→持久化全链路故障演练。
- [ ] 线程池饱和、任务积压、进程持续崩溃和资源限制下的恢复时限；业务告警平台接入。
- [ ] 1 万/10 万/100 万订单规模、状态筛选与冷热用户分布，EXPLAIN ANALYZE、同数据同页比较、HTTP 错误率和 P50/P95/P99。

当前允许说“新分页接口使用一次订单查询与一次明细批查；本机定向故障验证通过”。不允许说“吞吐提升 X%”“达到某 QPS”“生产可用”“跨系统 exactly-once”。模拟器是仓库协议演示，无真实物流对接。

## 4. 业务与运行边界

- 新分页是跨请求游标扫描，状态筛选会受并发更新影响；不宣称固定快照。旧完整列表仍可能返回较大响应，调用方迁移是后续工作。
- 老订单和非 PRODUCT 订单不自动登记履约。已出库后的退货、部分退款、真实收件地址/物流合同未实现。
- 不重复出库依赖仓库支持持久幂等、按键回查及命令摘要绑定；本地 fence 只阻止旧 Worker 覆盖本地结果，不能撤销已发出的外部请求。
- 未知结果禁止直接退款；重试保留原请求键。持续崩溃可能多次恢复租约，失败收尾才检查预算；仍需真实进程故障验收。
- 本轮没有修改 Agent/Context 源码或正式实验原件，没有提交、推送或执行破坏性 Git 操作。

隔离基线位于 `F:\agent\.runtime\backend-strengthening-20260904\baseline`；采集时 HEAD 为 `f0f1be5f5f6dbd347fad6cbd214cb5fddaa44129`，基线包含当时未提交修改。逐文件前后哈希及回写结果见 `evidence/delivery-manifest.json`（本地材料：`evidence/delivery-manifest.json`），可区分本轮变化与用户原有修改。初次失败日志保留在原隔离目录，未覆盖或伪装成通过。
