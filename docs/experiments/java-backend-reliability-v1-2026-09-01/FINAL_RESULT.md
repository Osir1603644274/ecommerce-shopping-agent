# Java 后端可靠性 V1 最终结果

结论：`BOUNDED_BACKEND_RELIABILITY_ACCEPT`。

## 完成内容

- 商品详情仍采用 Caffeine + Redis 两级缓存；写后提交阶段删除 Redis，并通过 Redis Pub/Sub 广播 `product:{id}`，使其他实例只失效本地 L1。
- 将固定窗口替换为 Redis ZSET + Lua 滑动窗口；写请求同时按 IP、JWT 用户和归一化接口计数，避免资源 ID 绕过及固定窗口边界双倍突刺；Redis 异常时退化为进程内滑动窗口。
- 订单过期的 `PENDING_PAYMENT -> EXPIRED` 条件更新、库存释放、优惠券释放及 `order.expired.v1` Outbox 写入处于同一事务；定时任务支持配置关闭。
- 秒杀保留 Lua 原子预扣、一人一单、Redis Stream、Pending 接管及补偿；死信写入改为按唯一事件幂等更新，避免死信已写后补偿或 ACK 失败导致重试卡死。

## 验收数字

- 当前 Java 全量回归：173/173 通过，0 failure，0 error，0 skipped。
- 真实 MySQL 8.4 + Redis 7：4/4 通过；14 个 Flyway migration 全部应用。
- 秒杀：200 个并发请求竞争 50 件库存，50 接受、150 售罄、Stream 50 条、超卖 0。
- 限流：100 个并发请求、额度 20，20 放行、80 拒绝；跨固定窗口边界的第三次请求被滑动窗口拒绝。
- 订单过期：2 个并发 worker 仅 1 个状态迁移成功，只产生 1 条过期 Outbox；预占 2 件后库存从 3 恢复到 5，reserved 从 2 回到 0。
- 多实例缓存：实例 B 先命中旧 L1；实例 A 删除并广播后，B 的本地项失效并读取 entityVersion=2 的新值。
- 秒杀死信：同一事件重复写 2 次只保留 1 行，attempts 更新为 9。

## 证据边界

- 这些是本机容器中的正确性、并发与故障恢复证据，不是生产容量压测，不提供 QPS/P95 宣称。
- Redis Pub/Sub 是 best-effort；广播失败时远端 Caffeine 最长仍可能保留到当前 30 秒 TTL。
- Redis 故障时限流退化为单进程窗口，不等价于跨实例全局限流。
- 订单超时仍是数据库轮询补偿，不宣称延迟消息的准点触达。
- 未引入 RocketMQ；Kafka 继续承担既有领域事件投影，秒杀命令链路使用 Redis Stream。
- 本次没有修改 Agent、Context、Multi-Agent 或长期记忆代码，没有模型调用，也没有创建 Agent 评测 attempt。
