# 订单查询与可靠履约 V1

本目录提供可独立于 Agent 运行的验证工具：Java 后端、专用 MySQL/Kafka/Redis、本地持久仓库和交易 fixture。
最新结果见 [后端强化验证报告](../../docs/experiments/backend-strengthening-v1-2026-09-04/FINAL_RESULT.md)。
不要对正在提供服务的数据库执行迁移或准备数据；同机并行时，性能数字按描述性结果解释。

## 功能与边界

- `GET /api/orders/page?size=20&status=PAID&cursor=...`：登录用户的游标分页；默认 20、上限 100；返回 `data.orders / nextCursor / hasMore`。
- 排序为 `created_at DESC,id DESC`；游标签名绑定用户、状态筛选。翻页不是跨请求数据库快照，期间状态改变仍会影响筛选结果。
- 旧 `GET /api/orders` 保持完整列表合同，明细分批查询；仍可能返回较大响应，调用端应逐步改用分页。
- 开启履约后创建的 PRODUCT 订单，在下单事务中登记 `WAITING_PAYMENT` 任务；旧订单、其他商品类型不自动补登记。
- 专用 Kafka 消费组把已支付任务置为 `READY`，Inbox 与任务同事务提交；订单权威状态对账也能恢复遗漏事件。
- Worker 使用有限线程池、持久化租约与递增 fence；HTTP 发生在数据库事务外。每次先按稳定请求键查询仓库，再决定是否发送原始命令。
- 仓库结果未知时进入 `UNKNOWN`，指数退避，默认 8 次后进入 `NEEDS_REVIEW`；需要人工重试。多次领取期间进程持续崩溃的租约恢复仍保留计数，失败收尾时检查预算。
- 退款与出库统一先锁订单。退款先赢则取消任务；出库开始或结果不明时拒绝直接退款。已出库退货/部分退款属于后续业务范围。
- 对已登记履约且确认未出库的订单，退款成功后通过库存 reservation 条件转移恢复可售库存；重复回调不能重复恢复。老订单缺乏未出库证明，不自动恢复库存。
- 客户确认收货前必须 `SHIPPED`；库存只沿既有支付链路确认，不因发货再次扣减。
- 真实仓库必须提供持久化幂等键、相同键不同命令拒绝、按键查询和匹配命令摘要的回执；否则本实现不能承诺不重复出库。

## 开关与接口

Spring 属性（可由外部 YAML 或环境变量绑定），默认均关闭：

```yaml
local-life:
  fulfillment:
    enabled: true
    worker-enabled: true
    kafka-enabled: true
    warehouse-url: http://127.0.0.1:19091
    warehouse-token: ${WAREHOUSE_TOKEN}
    request-timeout: PT2S
    lease: PT30S
    workers: 2
    queue-size: 16
    max-attempts: 8
```

- `GET /api/orders/{orderId}/fulfillment`：仅订单本人查询，不返回原始命令或内部错误。
- `POST /api/admin/fulfillment/{orderId}/retry`：管理员重新安排 `NEEDS_REVIEW` 对账，保留原请求键及命令，记录操作者。
- `POST /api/admin/fulfillment/events/{eventId}/replay`：管理员重放数据库死信原件，保留死信证据。`eventId` 是 `dead_letter_event.event_id`，不是原始消息 ID。
- Kafka 失败初次＋3 次重试后，先持久化 `FULFILLMENT_KAFKA` 死信再允许提交 offset；死信写入失败不能正常 ACK。
- `fulfillment_task` 查看积压状态与租约；`fulfillment_attempt` 查看领取、失败、出库、人工重试记录。Micrometer 提供 worker active/queued、queue rejected 和 dispatch 延迟/结果指标；告警平台尚未接入。

## 轻量验证

在独立后端副本使用 JDK 17+ 和 Maven Wrapper：

```powershell
.\mvnw.cmd -o '-Dtest=OrderPageIntegrationTests,FulfillmentIntegrationTests,FulfillmentCommitTests,WarehouseHttpTests,OrderServiceIntegrationTests,CommerceHttpContractTests,ModularityTests' test
python -m unittest discover -s scripts/backend-strengthening -p test_warehouse_simulator.py -v
```

Java 测试使用 H2；外部仓库协议测试使用本机 HTTP。Python 测试覆盖 SQLite 持久化、重启、并发幂等和提交后断开连接，不能替代真实 Kafka/MySQL 的提交及崩溃验证。

## 独立环境

`compose.yml` 与主项目 Compose 独立，使用专属数据卷和 loopback 端口；无需启动 Agent。默认项目名固定为 `backend-strengthening-v1`，同时只运行一个此类验证环境。新一组凭证用于全新环境，已有卷继续使用原凭证，不覆盖原始结果；不要使用 `down -v` 删除证据。

1. 使用 `python scripts/backend-strengthening/prepare_runtime.py --directory .runtime/backend-validation-NEW` 生成本地凭证（不打印密钥、不启动进程）。运行 `docker compose --env-file .runtime/backend-validation-NEW/compose.env -f scripts/backend-strengthening/compose.yml up -d mysql redis kafka`，等待服务可用。
2. 后端独立进程设置：`SERVER_PORT=38080`、`DB_HOST=127.0.0.1`、`DB_PORT=33316`、`DB_NAME=backend_strengthening`、`DB_USER=backend_test`、`DB_PASSWORD`、`REDIS_PORT=36389`、`KAFKA_BOOTSTRAP_SERVERS=127.0.0.1:39092`。
3. 使用独立随机 `JWT_SECRET`、`PAYMENT_CALLBACK_SECRET`；设置 `DOMAIN_EVENT_TOPIC=backend-strengthening.domain-events.v1`、`DOMAIN_EVENT_CONSUMER_GROUP=backend-strengthening-audit`；关闭秒杀、搜索等无关开关。订单/支付演示沿既有接口执行，本地模拟支付需显式启用。
4. 设置履约三开关，并设置独立 `WAREHOUSE_TOKEN`。将相同 token 存于 `.runtime` 下文件（至少 24 字符），启动仓库：

```powershell
python scripts/backend-strengthening/warehouse_simulator.py --database .runtime/backend-validation/warehouse.sqlite --token-file .runtime/backend-validation/warehouse-token.txt --port 19091 --fault-file .runtime/backend-validation/fault.json
```

5. 在 `backend` 目录执行 `.\mvnw.cmd package` 生成 JAR；回到项目根目录后，`docker compose --env-file .runtime/backend-validation-NEW/compose.env -f scripts/backend-strengthening/compose.yml --profile app up -d backend` 使用 JDK 17 运行独立后端。该容器配置了上面的独立连接，仓库经 `host.docker.internal:19091` 访问宿主机；内部出库验证使用 10 秒租约、2 次失败预算和 200 ms 轮询，业务默认仍为 30 秒、8 次和 1 秒。
6. 故障文件格式 `{"mode":"commit_then_drop_once"}` 可复现仓库提交后丢响应；`{"mode":"unavailable"}` 模拟不可用，`{"mode":"commit_then_delay"}` 提交后等待 10 秒，为进程强退提供窗口；`{"mode":"normal"}` 恢复。单次断开故障标记按请求键保存在仓库数据库。
7. 记录真实 Kafka 重复投递、消费者提交后退出、服务重启、数据库暂时不可用、退款/出库并发、线程池积压。核对仓库每单最多一条实际出库记录及数据库 Inbox/任务/尝试记录；禁止仅根据 HTTP 200 判定完成。

## 自动真实链路与 HTTP 负载

```powershell
python scripts/backend-strengthening/verify_real_backend.py --runtime .runtime/backend-validation-NEW --output .runtime/backend-validation-NEW/real-chain-attempt001
python scripts/backend-strengthening/benchmark_http_orders.py --runtime .runtime/backend-validation-NEW --output .runtime/backend-validation-NEW/http-attempt001 --duration 10
```

第一个脚本会创建独立账号/订单，重启并强退**专用 backend 容器**；注入专用数据库 Outbox 插入失败来验证事务回滚；检查真实 Kafka 消费、死信后的 offset、未知结果、管理员重试和库存。第二个脚本导入 1 万条仅供读取的历史订单 fixture，以 1/4/8 并发各测 10 秒，保存逐请求原始值；这些导入不产生真实库存、支付、履约副作用。结果目录必须不存在；失败后换目录，不回填原件。

## 查询对照

`order_query_benchmark.py` 创建全新的 `backend_bench_*` 数据库，拒绝默认 MySQL 端口。配置 JSON 放在 `.runtime` 下，字段为 `host`、`port`、`user`、`password`、`database`。账号须有创建此隔离数据库的权限，勿使用主项目数据。

```powershell
python scripts/backend-strengthening/order_query_benchmark.py --config .runtime/backend-validation/mysql-bench.json --output .runtime/backend-validation/query-attempt001 --orders 100000 --repetitions 10
```

对照相同 20 条订单和明细：N+1 为 21 次 SQL，批查为 2 次；校验完整内容摘要，交替执行、记录每次延迟与 MySQL 版本，输出首次/深页 EXPLAIN ANALYZE。数据模型保留查询字段和索引，简化非查询约束；这是 SQL 微基准，不能代表 HTTP 容量或生产 QPS。`--orders` 可选 1 万/10 万/100 万，每组使用新数据库和新输出目录。

`load/k6/order-pages.js` 另提供 k6 分页负载；需配置 `BACKEND_BASE_URL` 与已登录 `ORDER_BENCH_TOKEN`。本轮使用 Python HTTP 客户端实测，未执行 k6。性能报告应保存服务版本、数据规模、并发设置、错误率、P50/P95/P99、资源消耗及原始输出，不能把同机短测外推成生产容量。

## V2：读写混合对照与锁等待

执行口径见 [V2 计划](../../docs/experiments/backend-strengthening-v2-2026-09-05/PLAN.md)，实测及原件见 [V2 结果](../../docs/experiments/backend-strengthening-v2-2026-09-05/RESULT.md)。V2 使用新的 Docker 项目名和数据卷，沿用空闲的验证端口，因此不要与 V1 同时启动。

1. 准备当前批查版本 JAR，以及从同一源码副本只将 `OrderPageService.page` 明细读取替换为 `selected.stream().flatMap(order -> mapper.findItems(order.id()).stream())` 的 N+1 控制 JAR。控制副本保留游标、鉴权与交易逻辑；生产源码不回退。保存源码清单与唯一补丁，控制版不是历史版本。
2. 控制版有意不满足 `pageHasStableTiesAndUsesOnlyTwoMapperQueries` 的性能合同。针对其余 5 个方法执行语义测试并核对 XML 实际计数：完整遍历、游标签名、`emptyUserDoesNotQueryItemsAndLegacyResponseIsPreserved`、HTTP 分页鉴权及履约权限。Maven 方法名称写错可能仍然构建成功，不能只检查退出码。
3. `prepare_mixed_runtime.py` 需要 Python 的 PyYAML、PyMySQL 和已有 Docker 环境；运行时目录必须全新。它复制 JAR、生成随机凭证及专属 Compose，启动服务前可审阅 `compose.validation.json`。凭证和 `read-fixture-private.json` 只留 `.runtime`，不得复制到报告。

```powershell
python scripts/backend-strengthening/prepare_mixed_runtime.py --directory .runtime/backend-mixed-NEW --batch-jar PATH_TO_BATCH_JAR --control-jar PATH_TO_CONTROL_JAR
docker compose --env-file .runtime/backend-mixed-NEW/compose.env -f .runtime/backend-mixed-NEW/compose.validation.json up -d mysql redis kafka
python scripts/backend-strengthening/warehouse_simulator.py --database .runtime/backend-mixed-NEW/warehouse.sqlite --token-file .runtime/backend-mixed-NEW/warehouse-token.txt --port 19091 --fault-file .runtime/backend-mixed-NEW/fault.json
```

仓库在单独进程运行；等待 MySQL 完成初始化后，在另一终端执行：

```powershell
python scripts/backend-strengthening/run_mixed_suite.py --runtime .runtime/backend-mixed-NEW
python scripts/backend-strengthening/analyze_mixed_workload.py --runtime .runtime/backend-mixed-NEW --output .runtime/backend-mixed-NEW/analysis-attempt001.json
```

套件按登记次序执行 4 对混合负载及 1 次库存行锁注入。每阶段重建**V2 专用 backend**，不操作主项目或 V1 服务；只复用脚本/JAR 哈希匹配的成功结果，遇到失败即停止并保留原件，不覆盖重跑。

读取账户只有 10,000 条历史 fixture，写入账户/商品每阶段独立。写入使用真实创建、支付、本地回调与取消 API；记录实际读写比例。采集原始 Prometheus、MySQL 锁等待、Outbox/履约状态，结束后逐单检查库存与出库数量。脚本也会对本阶段专用商品持有 3 秒 MySQL 行锁，然后在 `finally` 回滚释放。

当前工具会保存 refresh token，在每阶段计时开始前通过真实刷新 API 轮换，防止多轮实验超过 15 分钟 access token 有效期。旧夹具若缺 refresh token，会明确拒绝继续；`renew_read_fixture.py` 提供仅限已验证 V2 合成账户的修复入口。认证准备不得混进计时窗口，若发生干扰应保留原件、注明原因并重做完整配对。分析器可用 `--selection PATH` 读取明确的主要/附加阶段清单；它会拒绝与已登记认证维护重叠的主要阶段。

V2 原始测量脚本与续期修复后的交付脚本哈希不同，报告保留了两个版本；修复分支单独通过两次真实 HTTP 刷新验证。恢复旧测量时不要绕过脚本哈希检查混用版本，应使用原执行快照核对原件或建立新运行目录。

指标分析必须保留所有配对、内容哈希和失败；连接池/队列峰值是采样观察值。未出现的指标记为不可用，不能当作零。数据只支持本机闭环客户端负载下的描述性结论，不是固定到达率压测或生产容量。结束后停止 V2 服务与本次仓库进程，保留数据卷，禁止 `down -v`。

## V3：双实例持续故障与独立审计

[V3 结果](../../docs/experiments/backend-strengthening-v3-2026-09-05/RESULT.md)保存固定运行器及全部原件。`prepare_fault_runtime.py --version v3` 生成专用配置，默认只准备、不启动；需要已有明确 SHA 的 JAR。V3/V4 共用验证端口，不能同时启动。重做实验必须新 runtime 和输出；当前准备器按新目录摘要生成独立卷名，避免复用同一个 Docker 项目的旧卷。历史冻结准备器无此改进，复现时必须人工核对卷名；V4 的第二次基础设施已显式使用新卷。

```powershell
python scripts/backend-strengthening/prepare_fault_runtime.py --directory .runtime/backend-fault-NEW --jar PATH_TO_IDENTIFIED_JAR --version v3
docker compose --env-file .runtime/backend-fault-NEW/compose.env -f .runtime/backend-fault-NEW/compose.validation.json up -d mysql redis kafka
python scripts/backend-strengthening/warehouse_simulator.py --database .runtime/backend-fault-NEW/warehouse.sqlite --token-file .runtime/backend-fault-NEW/warehouse-token.txt --fault-file .runtime/backend-fault-NEW/fault.json
```

等待 MySQL 可认证查询；仓库在独立进程运行，再执行：

```powershell
python scripts/backend-strengthening/verify_faults.py --runtime .runtime/backend-fault-NEW --output .runtime/backend-fault-NEW/backlog-attempt001 --case backlog
python scripts/backend-strengthening/verify_load_faults.py --runtime .runtime/backend-fault-NEW --output .runtime/backend-fault-NEW/redis-attempt001 --case redis
```

后者还支持 `kafka`、`database`、`warehouse`、`pause_recovery`、`payment_race`、`payment_boundary`，每项单独新目录。运行器验证 Docker 项目、主机 MySQL 端口对应 UUID，以及两实例容器内 JAR 哈希；会重建、暂停或停止这些专用服务，并注入专用数据行锁、Outbox 失败或仓库延迟。不得把配置指向主项目。

四类持续故障使用有限固定到达，记录 admission drop、client queue、flow latency、原始 HTTP、分实例指标、MySQL 等待、own Outbox/Inbox 与逐单库存/出库。自然终态和脚本付款补偿分开；预期支付拒绝控制也与负载错误分开。释放故障到最终核对含剩余负载，不当作精确 RTO。V3 只验证已登录 Redis 路径和履约 worker 饱和，不能扩写成所有 Redis 功能或 Hikari 耗尽。

## V4：多商品与按数量退款

使用 `prepare_fault_runtime.py --version v4` 建立另一个独立环境；仓库兼容旧单品命令及 `warehouse.cart.v2`。`verify_cart.py` 接受同样的 `--runtime/--output`，场景为：

- `legacy_seed`：实际 V15 JAR 上创建旧单；随后替换专用配置中两实例的 JAR 挂载，保留旧 JAR。
- `legacy_check`：用新 JAR 执行 V16，检查旧单取消、整单退款、库存及历史命令。若旧 seed 没有保存命令原字节，只能报告 V1 格式仍在，不能补造“逐字未变”证明。
- `rr_probe`：独立渠道回执提交后注入业务失败，12 个双实例重复对账，检查锁等待与数量/金额/库存副作用。RR 控制版本可能预期失败，原件仍必须保留。
- `full`：真实商品目录、多 SKU 订单、优惠分摊、退款幂等与权限、101+100+501 分退款、退款对账、逆序 SKU 并发、剩余商品履约。
- `default_disabled`：只在专用实例关闭履约，新购物车拒绝写入，旧单品创建/取消正常；完成后恢复配置。

V16 新表显式使用旧交易表的 `utf8mb4_unicode_ci`。MySQL DDL 失败会留下已执行的 ALTER；禁止把擦除失败卷或直接 repair 后成功当作升级通过，应保留失败数据，另建新卷从 V15 重走。

全部完成后先停止专用 app1/app2，再以 `export_fault_evidence.py --runtime ... --output NEW_DIRECTORY` 只读导出白名单业务表和仓库 SQLite 快照，最后停依赖和核实 PID 的本次仓库。运行凭证、JWT、refresh、私有 fixture 不进入正式证据包。Java/H2、真实 MySQL、独立重算、真实第三方渠道分别报告；生产履约开关不因此自动开启。
