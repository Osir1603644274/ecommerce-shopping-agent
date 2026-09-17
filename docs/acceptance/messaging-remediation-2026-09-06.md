# 消息机制修复与验证（2026-09-06）

目标是单机学习和实习展示。选型依据见[消息机制决策](../adr/messaging-selection-2026-09-06.md)。

## 实际替换

- 领域事件统一使用 Kafka，删除 Redis Streams 备选生产者、消费者；旧配置显式启动失败，避免静默切换或丢弃积压。
- 秒杀从“Redis Stream 是唯一受理记录”改成“MySQL 持久请求记录 + Redis Lua/Stream 通知 + SQL 恢复扫描”。返回 `QUEUED` 前请求记录必须提交；通知丢失后仍可恢复。
- 保留履约 MySQL 任务表、有界线程池和租约；保留缓存 Pub/Sub 广播。没有增加 RabbitMQ、RocketMQ，也没有给 Agent 推理主链路增加 MQ。

## 修复项目

| 优先级 | 问题 | 修复 |
| --- | --- | --- |
| P1 | Kafka 实际日志不在持久卷 | 指定 `KAFKA_LOG_DIRS`，停写复制并校验原日志；区分容器和宿主机监听地址 |
| P1 | Redis RDB 无法承诺秒杀受理持久性 | 开启 AOF；受理保证交给 MySQL 请求表，恢复扫描独立于 Redis 通知 |
| P1 | Inbox 的 PROCESSING 被当作已完成 | 抛出忙碌异常并继续重试；租约到期才重领，终态才跳过 |
| P1 | Kafka 通用错误恢复可能只记录日志 | 先保存消费组、topic、partition、offset、key、原始 value；SQL 失败不提交位点 |
| P1 | ACK 失败触发已落单订单的库存补偿 | 事务落单与 ACK 分离；补偿和落单共用活动行锁，并核对已提交订单及预约 orderId |
| P1 | Redis 领域事件备选不回收 Pending | 移除该运行路径；本机没有该路径的积压，不删除历史数据 |
| P1 | 评论旧 UPSERT 可能覆盖 DELETE | 每次投影读取当前 MySQL 状态并分配 revision；接收端持久 PREPARED/APPLIED 回执，拒绝旧 revision |
| P2 | 秒杀毒消息阻塞、Pending 首页饥饿、通知持续增长 | 逐条隔离并持久死信、分页 reclaim、终态 ACK 后只删除该条通知 |
| P2 | 恢复扫描中的补偿错误阻塞后续请求 | 失败补偿更新下次重试时间，继续恢复后面的请求；重建 Redis 时还原请求预约身份 |
| P2 | 缓存提交前删除或删除失败被吞掉 | 商户提交后失效；商户/商品事件投影重试 L2 删除并广播 L1 失效 |
| P2 | 评论 HTTP 调用无超时 | 连接 2 秒、读取 10 秒 |
| P2 | 可选记忆候选消费组启动失败、过期后 NOGROUP | 循环内重试建组，NOGROUP 后恢复；保留候选过期策略 |
| P2 | 运行检查发现商品重复删除持续报错 | ES 文档删除返回 `result=not_found` 视为成功；索引不存在、409 版本冲突仍失败 |

## 验证结果

本轮证据位于 `F:\agent\.runtime\mq-remediation-20260906-125601`，该目录已被 Git 忽略。

| 验证 | 结果与证据 |
| --- | --- |
| Java 全量单元/应用测试 | UTC JVM 下 256 项通过，0 失败；`java-suite-utc-results.json`、`java-verified-utc.log` |
| 真实 MySQL 8.4 / Redis 7 | 6 项通过，包含 Flyway V1–V17、SQL 恢复落单、重复投递、终态隔离、Lua 错误类型与预约补偿；`real-mysql-redis-tests.xml` |
| 补偿退避最后修改后的定向回归 | 19 项通过并重新打包；`java-final-targeted-results.json`、`java-final-package.log` |
| 商品删除与最终消费者修改 | 14 项通过并重新打包；`java-live-followup.log`、`java-live-followup-results.json` |
| Python 定向回归 | 19 + 17 项通过，覆盖记忆 worker、评论回执并发/崩溃与接口；`python-targeted.log`、`python-endpoints.log` |
| 现有数据库升级 | 将实际 V14 备份恢复到隔离 MySQL，再启动应用执行 V15–V17；关键表升级前后分别为订单 11、秒杀订单 450、Outbox 39、Inbox 139；`upgrade-application.log`、`upgrade-data-before.txt`、`upgrade-data-after.txt` |
| Kafka 数据迁移 | 原目录 286 个文件与持久卷逐文件 SHA256 一致；原消费位点保留，宿主机 AdminClient 成功访问 3 分区主题；`kafka-backup-sha256.json`、`kafka-volume-sha256.txt`、`kafka-host-probe.log` |

首次按本机默认时区运行时，支付过期测试有 2 项失败：测试使用 H2 的本地 `CURRENT_TIMESTAMP`，业务使用 UTC 时钟。明确指定 UTC 后通过。没有将默认时区下的失败写成通过，也没有修改支付业务或这些既有测试。

Java 全量验证命令（JDK 21，构建目标 Java 17）：

```powershell
cd F:\agent\backend
.\mvnw.cmd -o -q -Ptestcontainers '-Dit.test=FlashSaleRecoveryContainerIT' '-DargLine=-Duser.timezone=UTC' verify
```

容器测试选定本轮的 `FlashSaleRecoveryContainerIT`，不代表所有历史 ContainerIT 都已运行。Kafka 错误恢复验证使用真实 Spring ErrorHandler 和模拟消费者，验证持久死信先于 commit、数据库故障时不 commit；没有宣称完成真实 broker 全部崩溃窗口实验。

## 本机应用与边界

- Kafka 已迁入 `agent_kafka-data` 卷，Redis 已重建为启用 AOF 的配置；原秒杀 Stream 的 452 条历史记录保留。
- 后端采用经过测试的 JAR 更新，MySQL 已到 V17。V15、V16 是运行库尚未应用的既有迁移，本轮新增的是 V17。
- 原文件、旧后端镜像 `agent-backend:mq-before-20260906`、MySQL 两次备份、Redis RDB、Kafka 原日志均保留。最终包和运行状态以 `deployed-jar-sha256.json`、`runtime-final.json` 为准。
- 最终变更清单与每个文件的前后哈希见 `changes.json`，相对于本轮磁盘备份生成，不将原有脏工作区改动算成本轮修改。
- 当前没有运行 Agent 服务；其评论接收协议和可选记忆 worker 已改代码并通过定向测试，未启动模型或开展 Agent 评测。
- 单 broker、AOF everysec 均不是高可用或零数据丢失承诺。已受理秒杀以 MySQL 为恢复依据；`QUEUED` 仍不是最终落单成功。
- 评论回执文件应与所保护的索引共同保留；死信已有持久记录，本轮未增加自助重放管理后台。

回退时需先停止写入并核对新增受理请求与数据库版本；保留旧镜像不代表可以直接删除 V17 表或无损降级数据库。
