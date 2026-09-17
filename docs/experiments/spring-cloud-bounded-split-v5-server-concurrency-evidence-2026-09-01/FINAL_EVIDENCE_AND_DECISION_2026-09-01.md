# Spring Cloud 主线 B V5 纠偏证据与决定

## 结论

- 有界结论：`BOUNDED_SPRING_CLOUD_SPLIT_ACCEPT_WITH_SERVER_CONCURRENCY_OBSERVATION_AND_503_ATTRIBUTION`，正式场景 `10/10 PASS`。
- 生产默认：`HOLD_KEEP_MODULAR_MONOLITH_DEFAULT`。
- Java 17 全量回归：后端 `197/197`、网关 `4/4`，失败与错误均为 0。
- 本包不授权 QPS、容量、多主机、Redis Cluster、跨系统 exactly-once、生产就绪或生产默认切换结论。

## S09：四层并发证据

| 层次 | 正式观测 | 可用结论 |
|---|---:|---|
| 客户端任务 | 提交 200；两道 barrier 均通过 | 200 个客户端任务被释放 |
| 客户端 HTTP | 发起 200、完成 200、调用中峰值 199 | 不能改写为服务端 200 并发 |
| 服务端购买处理器 | 开始 200、完成 200、基线 0、活动峰值 7、结束 0 | 本次服务端实际峰值为 7 |
| JVM Redis 脚本调用边界 | 开始 200、完成 200、基线 0、调用中峰值 6、结束 0 | 表示本 JVM 围绕 `redisTemplate.execute` 的调用中数量 |
| 本地 Redis Lua 原子临界段 | 单本地 Redis、原子脚本执行并发语义为 1 | 不是 Redis Cluster 或多主机实测 |

V4 的 pre-HTTP 双 barrier `peakInFlight=200` 仅证明客户端任务在 HTTP 前同时就绪，不能作为服务端 200 并发、QPS 或容量证据。

业务不变量仍通过：库存 50 时 `202 x50`、`409 x150`、持久化 50、DB 库存 0、Redis 库存 0、超卖 0；成功用户重复请求后业务行仍为 1。

## S04：503 完整归因

- V4 不可变结果的正确解释：40 个请求均稳定返回 503；专用计数器只记录了 **4** 个 `BulkheadFullException`，其余 36 只能归为“下游失败或熔断短路”，V4 没有更细计数器，不能写成 40 个 Bulkhead 拒绝。
- V5 新正式运行：40 个 503 被服务端专用计数器完整分为 Bulkhead 5、下游失败 8、熔断短路 27。分布受实际到达时序影响，因此 V5 不强迫 V4 的 4 在新运行中复现。
- Bulkhead 仍为 semaphore、最大并发 8、等待 0；独立熔断场景仍完成 `OPEN -> HALF_OPEN -> CLOSED`。

## Feign 重试边界

当前唯一 `RemoteCommerceCatalogClient` 只有 catalog GET；`Retryer.NEVER_RETRY` 仅支持“当前 Feign 客户端禁用自动重试”。当前不存在远程写 Feign 方法，也没有远程写实验，故不扩展为“所有写操作均禁用重试”。

## 冻结与追溯

- V5 在最终预检通过且源码/实验契约不再变化后，冻结 manifest 并只运行一次 `attempt001`。
- manifest 绑定 321 个当前源/契约文件及 346 个 V1–V4 不可变前序文件；V1/V2/V3/V4 未覆盖。
- 关键 SHA-256：manifest `5ebe3a7ae740972dd296dd379a72525f52a5017871c4144481008a9666ebb22c`；runner `a77c4b5d77f2d3fb6028b1babec319ac5a8430d254585865ebfe978240d31aea`；result `89c003ebbb547a24b9c544c4fd727a7d0827f8f56ac9d75a10493c21bd0d2d17`；receipt `47db8e800ba848f2a8a3062132aba2690224ae02710c1531ee9d746c77470dc3`。
