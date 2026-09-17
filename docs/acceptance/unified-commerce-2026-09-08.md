# 统一电商前端实施记录

状态：LOCAL_FUNCTIONAL_ACCEPT（2026-09-09 00:04）。已完成约定统一前端及本地闭环验收；不是生产上线、推荐质量零幻觉或全仓测试通过的声明。下方保留执行过程和失败证据。

## 已确认范围

- React + TypeScript：AI 导购为中心，统一收藏、交易、订单入口；桌面并列、手机上下布局。
- 游客聊天，登录后收藏与交易；刷新恢复、账户隔离、游客登录衔接。
- 单商品可选数量；已有价格的二手手机配有限本地库存。库存不代表真实商家证据。
- 明确确认卡、本地模拟支付、超时回查与幂等；历史卡片须重新校验。
- 保留旧页面；不做购物车、评论、真实支付、推荐/记忆算法改造。

## 开始前审计

- 工作树有大量既有未提交改动；禁止覆盖或归因成本任务改动。
- 当前统一候选入口：Vite 5173 → 同源 BFF 8000 → Java 8080。18000 是旧独立演示入口，不作为新站依赖。
- Java 已有订单幂等键回查、支付自然业务键、库存条件扣减、订单分页。
- TransactionAgent 已有持久确认命令和超时对账，应复用而不是让浏览器直接拼交易请求。
- 旧浏览器导购直接传 sessionId，新增统一入口必须由服务器绑定会话，不能让浏览器冒用会话。
- 当前 MySQL 聚合：1506 个商品，其中 542 个带 verified 价格，964 个未配置价格；库存目前仅商品 17832 一条（耳机，available=8/reserved=0/sold=2）。这些是当前本地库状态，不是数据集真实性结论。
- 不覆盖 snapshotPriceMinor、priceStatus 或历史来源；二手手机交易使用已有价格与另外配置的有限本地库存。
- 既有 React 订单页保留其游标分页、明细和账户安全测试。新增收藏须以登录主体为所有者。

## 待验收

接口安全与恢复测试、前端构建与交互测试、实际浏览器桌面/移动端、少量真实模型导购及隔离账号下单/本地支付/回查。未完成前不得宣称全链路交付。

## 23:24 恢复执行后的实测与修复

用户重新开启目标，工具确认状态为 active；不重建目标、不缩小原范围。以下为新增局部验收，不覆盖未完成门禁。

- 历史商品和本人收藏现在可直接选择：服务器检查来源，重新读取商品价格/库存，再由 Java 生成确认预览。不要求用户重新搜索；任意商品编号、其他账户收藏、库存不足均被拒绝。
- 人工点击使用独立、服务端生成的 `browser-selection-*` 交易绑定，复用原 TransactionAgent 的认证、持久确认和 Java 幂等链路；不伪造 Agent 的 CandidateScope、不改推荐策略或旧 API 的候选校验。
- 浏览器确认记录按已认证 owner + 服务端会话稳定绑定，JWT 刷新/同账户重新登录不产生第二条命令；非浏览器旧流程继续按原凭据规则绑定。补测另一所有者无法确认，同 owner 换 token 只产生一次写入。
- 回查记录过期/消失不能证明数据库未提交；已进入 unknown 的交易不会因此解锁新下单。页面展示拒绝原因和重新预览入口，pending 时禁止聊天创建新交易。
- 新增完整购物空间 DTO 校验，缺失金额不显示成 0 元成功；SSE 会话拒绝时清空私有内容。同 requestId 不允许替换消息正文，完成重放不会再请求上游。
- `verify_workspace_model_chat.py` 已把输出文件存在检查移到任何 HTTP 请求之前，避免重复试验后才发现证据冲突。

### 当前测试证据

- Python 第一组：`test_commerce_workspace.py`、`test_transaction_agent_runtime.py`、`test_ecommerce_transactions.py`，33 passed。
- Python 第二组：`test_commerce_demo_api.py`、`test_commerce_demo_orders.py`、`test_transaction_capabilities.py`、`test_transaction_command_recovery_redis.py`，41 passed。合计 74；不代表全仓测试。
- Vitest：19 passed。TypeScript + Vite 构建通过；lint 0 error、7 warning（React effect/Fast Refresh 提示尚保留）。
- Playwright：22 passed，使用模拟 HTTP 服务，不依赖 MySQL/Docker，不等于真实交易验收。报告及已查看的桌面/手机截图：`unified-commerce-20260908-evidence/resume-2324/`。随后微调手机导航为横向，需另存最终视觉证据。
- 手机导航最终修复了旧样式的 `flex-wrap: wrap` 与按钮满宽冲突；新增三按钮处于同一行断言，统一版 5 个浏览器用例重跑通过。最终手机截图已查看，单独保存于 `unified-commerce-20260908-evidence/resume-mobile-final/`，不覆盖前一轮证据。
- 本轮没有真实模型调用、没有新增数据库账号/订单/支付。旧 `model-chat.json` 为确定性澄清回答，`modelCallsVerified=false`，不得称为模型验收成功。
- 曾误用不存在的 `test_commerce_demo.py` 导致一次 pytest 未收集测试；已定位正确文件并按上面两组重跑通过，不掩盖失败过程。

### 运行时阻碍与剩余门禁

- 23:13 尝试正常启动 Docker Desktop，日志确认启动失败：`initializing Inference manager ... dockerInference ... The file cannot be accessed by the system`。精确目标 `C:\Users\ming\AppData\Local\Docker\run\dockerInference` 为 ReparsePoint。未删除/重命名它，未恢复出厂设置，未重启整套 WSL，未清理数据库、镜像或卷。
- Docker 查询工具会话 24403 等待期间，后台日志已证明引擎停止并进入错误报告状态；仅取消本次挂起的 CLI 查询，没有当成容器仍在启动而反复重建。
- 最新代码尚未部署到容器；最新 Java 金额漂移防护镜像也需在恢复引擎后核对并部署。旧成功记录不能证明本轮代码的真实全链路。
- 待继续：Docker 局部安全恢复；全链路重新跑一次并验证数据库效果/令牌刷新恢复；真实模型调用与可用导购答复；多账号/多标签页与断线恢复的真实集成覆盖；最终桌面/移动视觉、交付清单与学习记录收口。目标仍 IN_PROGRESS。

## 23:30 无 Docker 补充验收

- 空 `outcome.result` 或空订单 ID 不再被当成成功回执；新增 2 项测试。Vitest 当前为 21 passed，构建通过。
- 扩展 `shop-live.spec.ts`：从本人收藏选择、数量 2、预览后退出并重新登录仍确认同一 confirmationId；模拟服务端已完成但浏览器丢失响应，检查前端恢复而不重复提交，最后检查单个已支付订单含 2 件商品。
- 上述真实脚本本轮仅被 Playwright 收集，尚未执行；不得声称新增场景已通过真实环境。先前已通过的旧 live 脚本不能代替新场景证据。
- 仍等待用户对 Docker 局部修复的明确授权。自动目标续跑不代替该授权；本轮未操作 Docker 运行时文件。

## 23:34 阶段核对（历史记录，非最终结论）

### 23:34 运行时状态更新

- 只读复核发现 Docker 已由外部操作恢复，CLI 可列出容器；本任务没有修改 dockerInference 或修复 Docker 文件。此前等待 Docker 修复授权的条件已随外部恢复消失。
- 仅启动已有 local-life-mysql/redis/kafka/elasticsearch，未启动实验容器。将 backend 更新为已通过测试的 `agent-backend` 镜像 `sha256:4c9e6d78f80b42452841dd3f4d09df5f22848816b5053c888babdd87b8723170`，保留原业务开关；源文件最新修改早于该镜像构建。Java readiness 实测 UP。
- 后续包含等待 readiness 与 `docker start local-life-agent` 的组合命令被执行策略拒绝（未执行）；单独只读检查确认 Java UP、Agent exited。没有通过替代执行路径重试被拒绝的启动动作。
- 当前仍缺 Agent 启动及真实全链路验证；以上恢复不等于验收完成。

| 目标要求 | 已有证据 | 仍缺少的证据 |
|---|---|---|
| 统一导购/收藏/订单，桌面与手机布局 | Shop 组件、22 项模拟浏览器回归与桌面/手机截图 | 最新真实数据三页面联合视觉回归 |
| 游客聊天、登录衔接、刷新恢复 | BFF 私有会话、guest adoption 与账户隔离测试 | 新版本真实登录/重新登录及跨标签页验证 |
| 商品来源与库存价格边界 | 服务端历史/收藏来源校验、二手手机 gate、库存脚本；Java 金额漂移测试先前构建通过 | 最新 Java 镜像部署、真实库存不足/价格改变拒绝的验证 |
| 单商品可选数量与显式确认 | UI 数量范围、Java 预览与持久确认绑定、mock 测试 | 扩展 live 脚本中的数量 2 和同卡重登场景 |
| 本地模拟支付与订单回查 | 双开关与 Java 所有权接口、旧版本 live 支付成功记录 | 当前代码真实完整交易与数据库效果核对 |
| 幂等、断线、unknown、账户隔离 | 74 项局部 Python 回归中的命令恢复与身份测试；SSE 身份变化清理、unknown 阻止新写 | 扩展 live 丢响应场景及真实跨账户拒绝 |
| 历史卡片重新校验、收藏再购买 | 历史/收藏最新商品读取和来源隔离单测、收藏直接预览浏览器测试 | 真实收藏重选与报价漂移回归 |
| 保留旧入口与数据、不改推荐、不接真实支付 | legacy-orders/demo 路由与原模拟浏览器回归；未清库/卷或修改推荐策略 | 运行时恢复后旧入口兼容检查 |
| 少量真实模型验收 | 已保存模型未调用的澄清回答，明确不冒充成功 | 有对应真实 modelCall/trace 且答复可用的一次模型导购 |
| 构建、测试、交付/学习记录 | 最新构建、Vitest 21、局部 Python 74、模拟浏览器及本记录、frontend README、review/progress.md | 全部门禁通过后最终收口；不提升学习者掌握等级 |

## 最终核对：2026-09-09 00:04

用户明确授权后，正常启动 `local-life-agent` 成功，未修改 Docker runtime socket、卷或数据库历史数据。统一入口为 `http://127.0.0.1:5173/`，隐藏 Vite 进程 37872；Agent 8000、Java 8080 可用。此为本机开发运行，不承诺重启后自动启动。

| 约定要求 | 当前证明与结论 |
|---|---|
| AI 导购、收藏、订单统一；桌面并列、手机上下 | `Shop.tsx`，最终真实导购/订单桌面截图及手机已支付截图；收藏桌面/手机截图已查看。导航和数据不需切三个应用。 |
| 游客聊天、登录衔接、刷新恢复 | 最终真实 live 用例从游客选商品到注册登录，保留选择/对话；退出并重新登录恢复相同确认卡，刷新后订单、收藏仍在。 |
| 已有价格的二手手机、有限本地库存、数量 | BFF `_card` 和 Java 真实预览；真实购买数量 2。安全用例请求 20 件库存不足被拒绝，不创建订单；未配置库存的候选可查看但购买按钮不可用。没有给未定价商品编造价格。 |
| 价格漂移与确认金额 | Java `changedConfirmationPriceRejectsWithoutOrderOrInventoryEffect`、`confirmationAmountParticipatesInIdempotencyIdentity`；部署镜像包含这两项，Dockerfile 在打包前执行 `mvn -q test`。本轮不通过改动现有商品价格来制造在线漂移；不能把集成测试说成在线改价试验。 |
| 显式确认、模拟支付、订单回查 | 最终真实 live 通过预览/确认订单、预览/确认支付、本地模拟成功、本人订单 PAID；MySQL 独立查询与页面金额、数量一致。没有真实扣款。 |
| 幂等、断线、unknown | live 让服务端完成订单后丢弃浏览器响应，只发 1 次订单确认，页面回查同一笔；模拟浏览器与 Python 另覆盖未决状态和回执消失，未决时不放行新写入。 |
| 账户隔离、多标签页 | 真实独立 A/B 账户：B 看不到 A 收藏，不能选择 A 历史/收藏、确认 A 的卡、读取他人订单、给他人订单预览支付或模拟付款；共享 Cookie 切换后旧标签页 CSRF 被拒绝并清空私人内容。 |
| 历史卡片、收藏重新校验 | Python 验证历史来源/价格刷新、收藏来源/库存变化；真实 live 从本人收藏直接重新选商品并预览，不要求重搜，不使用浏览器自填价格。 |
| 真实模型与前端接入 | `provider-call-verified.json`：真实 BFF handler（ASGI）+真实 Java/Redis/SDK 调用，1 次响应完成；请求 deepseek-chat，返回 deepseek-v4-flash，providerResponseId 已留存。不是浏览器抓包、不是模拟模型；不将单次调用成功扩大为推荐质量通过。 |
| 保留旧入口/数据、无越界改造 | `/legacy-orders`、`/?demo=1`、`8000/commerce-demo`、`8000/agent-flow` 均实测 HTTP 200，旧订单 17 个浏览器回归通过。没有修改推荐算法、接真实支付、实现评论/购物车或删除旧数据。 |
| 构建、验证、记录 | 构建通过；Vitest 22；Python 34+41=75；模拟浏览器 23；真实交易用例和真实安全用例各通过。README 与学习进度已更新，不提高掌握等级。 |

### 可复核证据

- `unified-commerce-20260908-evidence/live-resume-failure-1544/`：首次扩展 live 因“已选择/查看商品”按钮定位错误超时；只注册和收藏，未创建订单。修正定位，不修改测试预期业务结果。
- `live-resume-pass-1546/`：首次扩展 live 成功。账号 `workspace-qa-1788882323113`，订单 `aa111603-488f-4962-823b-a2dea7acd73c`，PAID，数量 2，应付 359560 分，支付 SUCCESS。
- `live-safety-pass-1550/`：真实账户/库存/跨标签页安全用例报告与收藏截图。
- `final-mock-2359/`：最终 23 个模拟浏览器用例报告及 Markdown 手机截图；不冒充实际模型回答。
- `final-live-0000/`：在 Markdown 与失败状态保护修改后重新跑真实闭环，通过。账号 `workspace-qa-1788883189409`，订单 `6c9d184b-4857-4933-a57a-cdfa95a16047`，同样仅 1 单、数量 2、359560 分、PAID/SUCCESS；桌面/手机已支付截图已查看。
- 两次成功 live 分属两个隔离账号，各一单。对应商品 2278548 最终库存 available=4/reserved=0/sold=6；其中本次恢复执行新增售出 4 件，恢复前已有 2 件，不把历史效果算成本轮。
- `provider-call-verified.json`：SDK 响应 ID `f6dbd3dd-56c6-4a98-9d52-fc3d1bd090ed`、finish=stop、输入 2806/输出 556 token；测试观察器只记录元数据，不替换请求/响应，不改运行中服务。
- 模型 trace 尝试 02/03 的 HTTP 返回仍为 `modelCallCounts={}`；这不能证明未调用模型。额外 SDK 观察弥补本次验收证据，不修改原有统计系统。

### 最后补充与已知边界

- 模型 Markdown 的标题、列表、表格现在安全渲染；禁用原始 HTML、模型输出的可点击链接和远程图片，交易只能走有权限校验的真实卡片。依据 [react-markdown](https://github.com/remarkjs/react-markdown) / [remark-gfm](https://github.com/remarkjs/remark-gfm) 官方文档实现，新增恶意 HTML/链接与手机表格测试。
- BFF 不再将上游错误 trace 中的诊断文字发布为成功回答，保留统一错误/重试；单测确保私有诊断不出现在回答或历史中。
- 真实模型比较回答仍出现“屏幕更大”等未被当前证据支持的推断。这是既有回答质量问题，**未通过推荐事实正确性验收**；没有将其写入商品事实、价格、库存或交易权限。此轮按约定不改推荐算法，前端功能验收不代表该问题已解决。
- 会话过期会要求重新登录；并未实现自动 Refresh Token 续期。相同所有者重新登录后的确认恢复已真实验证。
- 购物空间保存最近 60 条消息，TTL 14 天；收藏列表当前最多返回最近 200 项。没有宣称无限历史或无限收藏分页。
- lint 为 0 error、7 条 React effect/Fast Refresh 警告，未宣称零警告；未执行全仓测试或生产 HTTPS/容器前端部署验收。原始失败 trace 可能含测试登录数据，不作公开简历附件。
