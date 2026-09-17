# 后端强化 V4：多商品、按数量退款与真实 MySQL 结果

- 日期：2026-09-05；结论：`BOUNDED_MULTI_ITEM_AND_QUANTITY_REFUND_ACCEPT / HOLD_PRODUCTION_DEFAULT`。
- 新增购物车多商品、整数分优惠分摊、未派发商品按数量退款、独立渠道回执对账和不可变仓库命令版本；旧单商品、整单退款和 V15 订单继续兼容。
- 最终源码 JAR SHA256：`2230c74e0e56f7cc7a1de5df3b29f279332bcab23de85936bf6ac184bb1dab72`。338 项源码/资源与构建容器逐项匹配；根目录交付后又按同一清单逐项复核。没有修改 Agent，也没有提交或推送。

## 1. 实现合同

`POST /api/orders/cart` 接受 1～50 种 PRODUCT；同 SKU 重复拒绝，幂等摘要按 itemId 排序，所以请求顺序不同仍取回同一订单。总价和数量乘法使用精确整数，优惠按最大余数法分到订单行。

`POST /api/payments/orders/{orderId}/partial-refunds` 按 itemId/quantity 建退款单；本阶段只处理未派发的新购物车。创建退款后任务进入 REFUND_HOLD，匹配的本地渠道回执单独提交，业务对账再原子更新行余额、库存、命令版本、退款状态、订单状态和 Outbox。没有回执保持处理中；旧整单退款不能绕过新账本。

退款只可在 fence=0 且任务未领取时改写剩余商品；新命令使用新 revision/requestKey，旧命令字节保留。一旦领取、UNKNOWN 或 SHIPPED 就拒绝自动退款。生产默认关闭履约时，新的购物车写入在库存/优惠前返回 409；旧单商品接口仍可用。

## 2. 审查与真实失败

H2 和静态审查没有替代真实 MySQL。各失败 attempt 及其版本、数据原件均保留；迁移失败库单独保留，随后以新卷从 V15 重做升级。

| 发现 | 原始证据 | 修复及最终验证 |
|---|---|---|
| 默认关闭时购物车可创建却没有可用退款路径 | 独立源码审查 | 写前能力门禁；最终关闭配置下 409、0 购物车订单，旧单商品取消库存不变 |
| MySQL RR 下锁前普通读形成旧快照 | 控制 JAR 两次各 12 个并发对账，均 10×200 + 2×409；金额和库存仍只更新一次 | 对账显式 READ_COMMITTED，继续按订单锁串行；最终 12/12 SUCCESS、只退 1 件 101 分、命令 2 版 |
| V16 新表继承 0900 排序规则，与旧表 unicode_ci 外键不兼容 | 首次 V15→V16 报 MySQL 3780；失败库中 Flyway 16 为失败 | 五张新表显式 InnoDB/utf8mb4_unicode_ci；新卷从 V15 升级为 16 次成功、0 失败，旧订单可取消/整退、库存恢复 |
| 分摊外键先取库存 S 锁，再预占升级 X 锁 | 6 客户端逆序 SKU 并发：5 次创建、1 次取消真实 500；InnoDB 显示两事务都持 stock 8/9 S 并等 stock 8 X | `customer_order` 主记录写入后先按 stockId 预占，再写订单明细及带外键分摊；金额继续按 itemId 顺序对应。最终 12/12 创建取消、lock_deadlocks 6→6 |
| `order.partial-refunded.v2` 未注册消费者 | 6 条合法事件各重试 8 次进入通用投影死信 | 常量、通用投影和专用履约消费者同时登记；最终主场景本身 32 Outbox、64 条双消费者收据、0 自身死信 |
| 验收脚本只看成功标记、锁等待归因过宽 | 独立审查构造缺证据/篡改负例；MySQL 8.4 又证明旧 deadlock 状态项不存在 | 逐单财务/库存/SQLite/事件重算；目标 blocker＋订单主键锁归因；使用已启用的 INNODB_METRICS.lock_deadlocks，缺指标直接失败 |

首次完整交易的 7 个 500 中，1 个是预期的退款 Outbox 注入，6 个是并发死锁，已经按逐请求 `expectedStatus` 分开。缺失的 MySQL 状态项没有填作 0。失败卷和失败时 SQL/SQLite 导出均在证据包。

## 3. 测试、金额和并发结果

最终 Java 17 Surefire XML 为 **242/242，0 failure/error/skipped**：原 239 项，加购物车 Outbox 失败整体回滚 1 项、部分退款事件双消费者幂等/冲突及迟到保护 2 项。中间锁修复版 240 项也保留。仓库模拟器合同 **7/7**；独立直接编译生产 `MoneyAllocation` 的检查通过 41,743 组优惠分摊、738,815 组分段退款及 31 个边界组合。这些组合数不写成 JUnit 数量。

真实 MySQL/Kafka/双 Java 实例的最终完整场景：

- 不足库存时订单、第一商品预占、优惠券和 Outbox 整体回滚；V16 成功。
- 两行小计 303 / 404 分，优惠分摊 2 / 3 分，实付 301 / 401 分；三次退款 **101 + 100 + 501 = 702 分**，全退后库存、行余额和支付金额守恒。
- 回执已提交、业务 Outbox 注入失败时返回预期 500，退款仍 PROCESSING、任务 REFUND_HOLD，金额/库存/命令不变；随后 12 个双实例对账请求全部取回 SUCCESS，只产生一次副作用。
- 6 客户端同时创建并取消 12 笔双商品订单，请求 SKU 顺序交替反转；12/12 成功，实验区间死锁计数增量 0。
- 共 14 笔主场景订单：12 CANCELLED、1 REFUNDED、1 PAID/SHIPPED。后一单先退 1 件，实际仓库命令仅含剩余 2+2 件；两 SKU 最终均 available 99,998 / reserved 0 / sold 2。
- 63 次 HTTP：24×201、32×200；5×409、1×403、1×500 都是登记负例，非预期失败 0。
- 14 单产生 32 条自身 Outbox；每条分别对应唯一 `fulfillment-v1` 和 `domain-event-projection` PROCESSED Inbox，64 条收据的事件身份和 payload hash 均匹配，0 条自身 DLQ。

RR 专项新 attempt 另有 18 次 HTTP，唯一 500 是登记的业务 Outbox 注入；其余均成功。最终 SQL 导出包含 31 单，其中还含 seed、开发和失败 attempt；上述主结果只按正式 case 保存的订单 ID 计算，不把全库混成一个主要数字。

旧订单迁移实测可证明旧 V1 命令形状、订单终态及库存正确；最初 seed 没保存命令原字节，因此明确保留 `commandByteEqualityVerified=false`，不补造升级前后字节相等的证据。

## 4. 证据身份与边界

- 正式证据清单（本地材料：`evidence/manifest.json`）包含最终/失败 attempt、三个源码版本、JAR、测试 XML、数据库 schema、只读 SQL/SQLite 导出和两路独立审查，不含运行凭证。
- 最终独立证据审计（本地材料：`evidence/audit-evidence/FINAL_REPORT.md`）、独立代码关闭审查（本地材料：`evidence/audit-code/V4_FINAL_REVIEW.md`）、交付回执（本地材料：`evidence/delivery-receipt.json`）。
- [执行计划](PLAN.md)、[工具说明](../../../scripts/backend-strengthening/README.md)、[学习日志阶段十二](../../learning-notes/backend-excellence-journal.md#backend-cart-refunds-20260905)。

结论只覆盖本机有界合成订单、本地支付退款回执和持久仓库模拟器。没有真实支付服务商退款通知、渠道账单对账、已出库退货、长时间容量或多物理机证据；生产履约默认继续关闭。V3 的 Redis/Kafka/行锁/队列故障矩阵属于支付修复版本，未冒充最终 V4 JAR 的整套重跑。
