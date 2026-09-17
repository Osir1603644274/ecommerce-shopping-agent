# 消息机制选型与修复（2026-09-06）

目标：兼顾 Java 后端与 Agent 应用日常实习展示，部署在一台电脑。以真实业务语义、故障可复现和维护成本决定组件。

## 决策

| 场景 | 选择 | 原因与边界 |
|---|---|---|
| 订单事件、检索投影、评论同步、履约事件 | Kafka + MySQL Outbox/Inbox | 已有不同订阅者、重放与派生数据恢复需求；保留单 broker 演示配置，不声称高可用 |
| 领域事件 Redis Streams 备选 | 退出运行代码，配置显式拒绝 | 两种传输此前可靠性不等价；保留旧源码备份和旧数据，迁移前导出、重放核对 |
| 秒杀受理 | MySQL 请求记录 + Redis Lua/Streams 通知 + SQL 恢复扫描 | 返回 QUEUED 前必须提交请求记录；Stream 不再是唯一恢复依据。保留 Redis 内原子扣减/占位/入队 |
| 发货执行 | MySQL 任务表 + 有界线程池 + 租约/fence | 延迟、重试、仓库回查已由任务状态机管理；Kafka 是事件接入，不是执行线程池 |
| 缓存与本地索引广播 | Redis Pub/Sub + 可重建投影 | 每个实例都需要通知，不能直接替换成共享竞争消费组 |
| 记忆候选 | 有期限的 Redis Streams，默认关闭 | 属于可过期候选，非交易命令；修复启动故障与 NOGROUP 恢复，不增加 Agent 主链路 MQ |

RabbitMQ 的手动确认、消费分配和死信机制适合独立工作队列；本项目的履约重试仍需 MySQL 状态机，秒杀直接跨 broker 发送又需要解决数据库/Redis/broker 之间的交接，因此本轮未引入第三个常驻服务。这是当前约束下的选择，不是 Kafka 普遍更优的结论。RocketMQ 同样没有必须采用的专属需求。

## 修复合同

- Inbox 的 PROCESSING 不是成功收据；未过期 claim 触发重试，租约过期后才重领。
- Kafka 通用错误恢复先持久保存 topic、partition、offset、key 和原始 value；数据库失败时不确认消息。
- 秒杀 SQL 事务和 ACK 失败分开；库存释放必须确认没有已提交订单，并校验 reservation 的 orderId。
- 秒杀毒消息逐条隔离；Pending 分页回收；终态 ACK 后删除对应通知，保留未消费和 Pending 消息。
- 返回 QUEUED 表示请求已持久受理，最终订单仍需查询；响应丢失/数据库不可用时结果可能不确定，不能把异常解释为一定未落单。
- 评论事件改为读取最新 MySQL 状态，持久分配 projection revision；接收端 SQLite 先保存 PREPARED，再串行更新索引，防止旧请求覆盖删除。回执文件必须与被保护索引一同保留。
- 商户提交后失效；商户/商品 Outbox 投影可靠删除 L2 并广播 L1 失效，删除错误进入事件重试。

## 验证与迁移

验证结果见[修复与验证](../acceptance/messaging-remediation-2026-09-06.md)。本机原文件保存在 `.runtime/mq-remediation-20260906-125601/before/`，原始 SHA256 在同目录的 `before-sha256.json`。既有改动不归属于本轮。

Kafka 根 Compose 原卷挂在 `/var/lib/kafka/data`，实际日志在 `/tmp/kafka-logs`；必须先停写、停止 broker、复制日志并核对，再以修复后配置启动，不能直接重建旧容器。Redis AOF 开启需要等待初次重写完成；AOF everysec 仍有故障窗口，秒杀恢复依赖 MySQL 已受理记录。

资料：[Kafka 3.9 设计](https://kafka.apache.org/39/design/design/)、[RabbitMQ Quorum Queues](https://www.rabbitmq.com/docs/quorum-queues)、[Redis Pub/Sub](https://redis.io/docs/latest/develop/pubsub/)、[Spring Kafka 错误处理](https://docs.spring.io/spring-kafka/reference/kafka/annotation-error-handling.html)。
