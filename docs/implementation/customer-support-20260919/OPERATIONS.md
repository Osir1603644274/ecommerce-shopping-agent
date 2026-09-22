# 客服模块运行交接（工程验收已通过；运行交接仍需按当前机器状态核对）

本手册描述已有隔离环境的启动与核对，尚非全新机器一键部署。P0–P7固定套件已在 suite-fixed-006 完成工程验收；独立AGENT复核已导入，但人工标签仍是UNREVIEWED，不称人工金标或生产就绪。

## 服务边界

| 入口 | 本轮用途 |
|---|---|
| 5173 | 客户界面；使用run_support_frontend.ps1时代理18000，需显式启动 |
| 8000 | 原有BFF；不能因页面返回200就认定其Java配置正确 |
| 18000 → 18080 | 隔离客服BFF → Java权威服务 |
| 18001–18009 | 评测器自管临时BFF，结束回收，只终止自有进程树 |
| 13319 / 16379 | 独立MySQL / Redis，本机绑定 |
| 19093 | 独立管理员模拟控制台；按需前台启动，客户模型不持有其令牌 |

已有运行配置为`runtime/live-001/private.json`，包含数据库凭据，仅留本机。不要把runtime、私有浏览器fixture、test.env或JWT加入证据包。业务数据必须是新建合成账号/商品/订单，不借用真实账户。

## 启动与观察

当前源码演示可使用以下前台入口（要求独立MySQL/Redis已运行、当前Java jar已构建，以及本机私有配置存在）：

```powershell
./.venv/Scripts/python.exe scripts/customer_support/serve_demo.py --runtime docs/implementation/customer-support-20260919/runtime/NEW_DEMO
```

每次使用新runtime目录，创建新的support_demo数据库，不清除旧数据。启动当前jar的18080、当前Python源码的18000和5173，端口被占即拒绝；Ctrl+C或任一子服务退出时仅回收本次进程树。PUBLIC_STATE.json记录可公开的PID/端口与jar摘要；private.json及日志仍留本机。此启动器不自动创建演示账号或启动管理员控制台，不表示全新机器部署或业务验收完成。旧Docker Java快照不得被误认为当前构建。

升级后保留原演示数据，可在旧自有服务已停止后追加`--reuse-database-from <旧runtime目录>`，同时仍指定新的`--runtime`目录保存新日志；仅接受ownedNativeRuntime=true的本机support_demo库，不覆盖旧配置和日志。

在F:/agent运行以下只读探针，输出文件必须不存在：

```powershell
./.venv/Scripts/python.exe scripts/customer_support/preflight.py --runtime docs/implementation/customer-support-20260919/runtime/live-001 --output docs/implementation/customer-support-20260919/evidence/NEW_PREFLIGHT.json

当前最终入口预检见 `evidence/preflight-final-demo-003/PREFLIGHT.json`；该记录显示四个宿主入口可达，同时记录 live-001 Docker 元数据为 exited，不能把宿主端口可达误读为容器正在运行。
```

探针只报告容器状态、白名单开关、迁移状态和HTTP可达性，不打印凭据。HTTP 200只证明该路径响应，不证明登录、业务权限或模型执行正确。若容器已运行，不因观察超时重复启动；先核对具体进程/容器终态。

隔离Java容器为`support-live-20260919-01`，标签`support.attempt=live-001`。其运行需要SUPPORT_ENABLED、SUPPORT_SIMULATOR_ENABLED、FULFILLMENT_ENABLED和PAYMENT_SIMULATOR_ENABLED为true；本轮主动模拟由脚本推进，因此自动FULFILLMENT_WORKER与SUPPORT_RECOVERY为false。实际环境变量全名见探针白名单。不要开启worker同时驱动固定场景。

Java、MySQL和Redis可用后，在独立终端前台运行：

```powershell
./scripts/customer_support/run_live_bff.ps1
```

如需让5173连接隔离客服，先由当前5173进程的持有者正常停止该进程，再运行：

```powershell
./scripts/customer_support/run_support_frontend.ps1
```

脚本使用strictPort；端口被占时直接报错，不杀已有服务、不写.env。退出该前台进程后，可在原终端按原有配置恢复5173→8000。当前默认8000的权威服务问题尚未纳入修复，不将隔离环境的成功外推到默认环境。

## 演示顺序与凭据

用户登录5173，查看本人订单，付款后仍未收货；管理员在独立19093或固定脚本推进原订单出库/签收。签收后七天内，用户选择商品和数量，查看预览并点击确认。仅退款由管理员审核后生成并应用退款渠道回执；退货退款/同规格换货先由用户登记寄回，再独立收货验收；换货验收后占库、出库、签收。页面刷新查询Java事实，模型不能代替这些外部事件。

启动独立控制台、四条固定脚本、原幂等键与journal重放方法见模拟器说明（本地材料：`../../../scripts/customer_support/README.md`）。管理员JWT仅通过环境或runtime私有文件传入，绝不写进客服提示、浏览器源码和模型工具。控制台用完终止自己的前台进程。

## 恢复与停止

未知库存、回执延迟、NEEDS_REVIEW必须保留原记录。核对原回执/原命令，使用原标识重试；不生成“成功”来清除报错。关闭工单不表示退款到账。换货已有独立出库回执时不释放预占，未确认释放不能改退款。失败重复8次进入核实，人工处理仍走原验证。

暂停新客服对话可关闭BFF的CUSTOMER_SUPPORT_AGENT_ENABLED；Java售后、模拟器和后台恢复分别由开关控制。关闭开关不是业务回滚，不删除售后、库存占用、回执或迁移。关闭后的旧模块兼容与在途恢复行为仍待专门验收，不能凭配置默认值推定已通过。

## 证据与验收边界

每次评测使用新输出目录和新业务fixture；保留失败、源码哈希、SQL前后快照、独立判分及全部模型attempt。优先阅读STATUS.md、EVALUATION.md和各次REPORT；不要相加不同代码版本的通过数。当前固定三轮使用dataset-draft-008；`evidence/suite-fixed-006`保存原始运行，`evidence/suite-fixed-006-agent-import-002`保存独立AGENT复核汇总。数据仍是合成评测集，heldout是保留草案，不称未知盲测；人工标签仍未完成。

## 固定版本三轮评测

完整执行入口为run_suite.py，每轮依次运行dev/local 120条、heldout/local 104条、heldout/remote 10条、heldout/logistics 4条和heldout/receipt-retry 2条，共15批、720次观察。各批新建隔离数据库及自有Java/BFF进程，结束回收自有进程；共享MySQL、Redis和独立库存模拟器需事先就绪。此入口不启动用户5173界面。

```powershell
./.venv/Scripts/python.exe scripts/customer_support/run_suite.py --dataset docs/implementation/customer-support-20260919/evaluation/dataset-draft-008/scenarios.jsonl --runtime docs/implementation/customer-support-20260919/runtime/NEW_SUITE --output docs/implementation/customer-support-20260919/evidence/NEW_SUITE
```

runtime和output均使用未占用的新目录。执行期间不得修改应用、评测脚本、数据集、Java jar或相关配置；每批前后核验版本指纹。发生进程错误或版本漂移时停止后续批次，缺失场景保留在720分母中，不自动覆盖或续写旧尝试。模型语义失败按原判分保留，不因个别FAIL重启整个执行器。STATE.json记录批次进度，REPORT.json以实际生成文件为准；中间PASS不表示整体验收。

suite-fixed-006已完成15批、720次观察；原始报告和独立AGENT复核汇总见DELIVERY.md。导入器只验证证据绑定与表单完整性，不能认证审核者身份，也不自动生成金标。

已有浏览器基础链路及独立控制台通过的范围见STATUS；独立套件和入口预检已保存。仍需人工逐断言审核、真实部署环境验收和生产SLA验证；证据包必须排除runtime及密钥，并在发布前重新核对清单哈希。
