# 隔离 MySQL / Java 17 验证

本次运行编号：`mysql-java17-001`。测试继承 `SupportServiceIntegrationTests` 的全部业务断言，通过 `SupportMysqlAcceptanceIT` 的 DynamicPropertySource 切换到真实 MySQL，关闭测试 schema.sql 初始化、开启实际 Flyway 迁移。

- MySQL 容器：`support-mysql-20260919-01`，镜像 `mysql:8.4`，独立数据库 `support_acceptance`。
- 构建容器：`support-java17-20260919-01`，镜像 `maven:3.9.16-eclipse-temurin-17`。
- 独立网络：`support-acceptance-20260919`；MySQL 仅映射本机 `127.0.0.1:13319`。
- 代码副本及报告：`runtime/mysql-java17-001/backend/`、`runtime/mysql-java17-001/reports/`。副本建立后源目录变化不会自动进入本次验证。
- 测试命令：`mvn -B -Dtest=SupportMysqlAcceptanceIT test -q`。
- 随机隔离测试凭据在 runtime 的 `test.env`，不纳入交付证据，不输出到报告。

恢复观察时先运行 `docker inspect --format '{{.State.Status}} {{.State.ExitCode}}' support-java17-20260919-01`，再读 `docker logs --tail 50 support-java17-20260919-01`。运行中不得因为观察超时而重启。终态后保存原日志及 surefire XML，核查测试分母、失败和跳过，不能将进程退出或数据库启动等同业务验收通过。

本轮没有启动、迁移或删除旧业务容器，没有改写原 backend/target。暂时保留本轮容器与数据库供后续失败诊断和网络故障测试；处理完成后仅清理这个运行编号创建的资源。

## 第二轮

`mysql-java17-002` 已建立新的源码副本、数据库 `support_acceptance_002` 和容器 `support-java17-20260919-02`，复用同一隔离MySQL服务及网络。副本包含排序规则修正、工单与V31原订单物流；不包含此后新增的V32/V33恢复与模拟时钟。启动命令与首轮相同，报告位于新的 runtime 目录，仍须等待终态才能判断。首轮退出1，28项因V23外键排序规则导致上下文错误，证据已封存，不能覆盖。

第二轮已退出1：31项中1失败、15错误，其余15项通过。已完成全部31个Flyway迁移。业务问题定位为completed_at秒级四舍五入和可重复读快照导致的并发确认旧读；完整报告在 evidence/mysql-java17-002/。

## 第三轮

`mysql-java17-003` / `support-java17-20260919-03` / `support_acceptance_003` 使用修订源码，包含V34时间精度及新售后事务READ_COMMITTED。`runtime/mysql-java17-003/run.sh` 将源码复制到容器原生 `/work`，依赖缓存放在独立 `support-maven-cache-20260919` 卷，从只读Windows缓存播种一次；不覆盖旧源码快照。结束时测试报告复制到该runtime的reports目录。容器日志与State是运行状态权威，尚未终态不得宣称通过。

第三轮终态退出1：33项，28通过、5错误、0断言失败。34个Flyway迁移全部成功。两处测试fixture使用H2专属DATEADD导致MySQL语法错误；三处模拟原订单出库因履约claim读取CURRENT_TIMESTAMP只有秒精度，而next_attempt_at为微秒精度，刚设置的立即执行任务被判为尚未到期。真实SQL探针验证同一条语句CURRENT_TIMESTAMP(6)>CURRENT_TIMESTAMP为1。日志、报告与源码哈希封存在evidence/mysql-java17-003/。

## 第四轮

使用新数据库support_acceptance_004、容器support-java17-20260919-04、源码副本runtime/mysql-java17-004。修订履约到期比较、claim与失败退避的当前时间为CURRENT_TIMESTAMP(6)；测试过期fixture改为绑定Timestamp参数。包含独立控制台的管理员状态查询、指定单库存推进及模拟器开关。继续复用已播种的专用缓存卷，不覆盖前轮数据或报告。此轮需等待真实终态。

第四轮终态退出0：34项全部通过，失败/错误/跳过均0；34个实际迁移全部成功。报告与快照哈希封存在evidence/mysql-java17-004/。四轮证据均保留。该轮使用模拟catalog和本地库存分支，独立库存服务网络故障与真实浏览器链路不在此次34项分母内。

## 第五轮

新的runtime/mysql-java17-005、support_acceptance_005数据库及support-java17-20260919-05容器包含重试上限、工单核实恢复、30天换货预占释放及到期禁止新出库回执。复用独立缓存和网络，保留旧快照；正在运行，不能预先认定通过。

第五轮终态退出0：38项全部通过，失败/错误/跳过均0；34个迁移成功。证据在evidence/mysql-java17-005/，包括源码快照哈希。覆盖本地库存分支，跨服务网络与Agent质量仍待验收。
