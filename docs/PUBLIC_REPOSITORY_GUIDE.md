# 公开仓库与运行指南

2026-09-17 公开源码更新；以下运行验收来自 2026-09-15：独立搜索、商品 / 交易服务及库存服务已完成本机验收，见[发布记录](architecture/microservices-release-20260915/RELEASE.md)。本次同步源码与精选报告，不附带私有部署清单、原始数据、数据库或权重。

状态：`CURRENT_PUBLIC_GUIDE`  
适用范围：公开仓库 <https://github.com/Osir1603644274/ecommerce-shopping-agent>。

## 十分钟阅读路径

1. 根目录 `README.md`：产品主线、快速启动和诚实边界。
2. `docs/diagrams/current-architecture/README.md`：当前全系统图与可交互单步图。
3. `docs/FEATURE_EVIDENCE_INDEX.md`：每项能力对应的源码、测试和证据。
4. `agent/app/static/commerce-demo.html`：可操作的 Agent→Java 交易闭环。

## 代码边界

- `agent/app`：生产 Agent 运行时。模型只能选择服务端发布的受限动作；高风险写操作移交 TransactionAgent。
- `backend/src`：商品与交易共享构建产物，可按职责配置分别运行。MySQL 保存交易权威事实，Redis 保存缓存/会话/幂等状态，Kafka 传递 Outbox 事件，Elasticsearch 保存可重建检索投影。
- `backend-gateway/src`：当前本机服务部署的网关；`backend-inventory/src` 为独立库存工程。
- `agent/evaluation` 与根目录 `evaluation`：离线 runner 和证据产物；生产 `agent/app` 不得反向导入。
- `retrieval_judgment_pool_core`、`retrieval_judgment_pool_mcp`：候选池工具链，与在线 Agent 解耦。
- `docs/legacy` 与历史兼容接口：仅复现旧本地生活/RAG 路线，不进入当前自动路由。

## 本地 Demo

1. 安装并启动 Docker Desktop。
2. 安装 PowerShell 7，确认命令为 `pwsh`。
3. 复制 `.env.example` 为 `.env`，仅在本地填写 `DEEPSEEK_API_KEY`。
4. 执行：

```powershell
pwsh -NoProfile -File .\scripts\commerce-demo.ps1 start
pwsh -NoProfile -File .\scripts\commerce-demo.ps1 smoke
```

脚本显式开启三个本地演示开关；普通配置中的 TransactionAgent、Demo BFF 和支付模拟均保持关闭。支付回调是本地模拟，不连接真实支付渠道。

若已有同名共享容器在运行且状态不完整，脚本会拒绝重新配置。成功启动后会记录本次拥有的容器；`stop` 只停止这些容器，不删除卷。

## 测试边界

- Agent 定向测试：BFF 会话/CSRF、交易预览/确认、TransactionAgent、静态页面合同。
- Java Docker 构建：构建阶段执行 Maven 测试后才生成运行镜像。
- Smoke：必须真实创建用户、订单和支付记录，并由 Agent 回查 Java/MySQL 的 `PAID`。
- 这些检查不证明生产容量、多机容灾、真实支付或跨系统 Exactly-once。

## 公开候选快照

`scripts/build-public-snapshot.ps1` 采用允许列表复制源码和必要文档，并排除：

- `.env`、本地缓存、IDE 文件和构建产物；
- 外部原始数据、模型缓存、运行日志和临时文件；
- sealed 评测、review bundle、旧交接包和个人输出；
- 大于 5 MiB 的单文件及常见真实密钥格式。

输出目录必须为空或不存在，脚本不会覆盖已有快照。生成后以 `PUBLIC_SNAPSHOT_MANIFEST.json` 固定相对路径、大小和 SHA-256。

## 发布流程

1. 在新的 `.runtime/public-snapshot-v*` 目录生成允许列表快照，禁止覆盖旧版本。
2. 独立复算文件集合、大小、常见密钥模式与 SHA-256 manifest。
3. 在快照内部执行仓库卫生、Markdown 链接与定向测试。
4. 只从该快照创建独立 Git 历史并推送；不得直接发布受保护的本地工作树。
5. 推送后重新下载远程归档，复算 manifest 并抽查首页、动图和文档链接。
6. GitHub Actions 只读复查 manifest、仓库卫生和活动 Markdown 链接，不运行模型或读取外部数据。

本仓库用于个人作品展示。第三方依赖和外部数据遵循各自许可证；原始外部数据不随仓库分发。
