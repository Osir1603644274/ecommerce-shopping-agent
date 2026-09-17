# 拾物 · 统一购物空间

React + TypeScript + Vite。主页统一 AI 导购、我的收藏、交易确认和我的订单。旧订单页与 commerce-demo 保留；不接真实支付、不做购物车和评论，不改推荐算法。

统一版已完成本地功能闭环验收（2026-09-09），详见 [实施与验收记录](../docs/acceptance/unified-commerce-2026-09-08.md)。不是生产上线或推荐内容零幻觉声明；实际模型仍有个别无证据推断，未改推荐算法。

## 使用

日常统一入口（复用本机已验收的容器与数据，不重建、不清库）：

```powershell
cd F:\agent
pwsh -File scripts/unified-commerce.ps1 start
pwsh -File scripts/unified-commerce.ps1 health
```

固定链路：`5173 React → 8000 Agent/BFF → 8080 Java → MySQL/Redis/Elasticsearch/Kafka`。
脚本已转接 `scripts/merged-commerce.ps1`，校验目录与 Docker Redis 身份；本机 Agent 和前端隐藏运行，Java 与仓库模拟器在 Docker 中运行。
`stop` 停止本脚本拥有的前端和 Agent，保留数据库及回执。不是开机自启或生产部署。
依赖缺失、容器配置不符或端口被其他程序占用时会停止并报错，不擅自重建或杀进程。
439 款二手手机已复制入主 Java 目录，模拟售价存在独立报价表，每款库存首次初始化为 10；重复启动和重复迁移不会补货。原数据集报价不变，旧商品仅退出新导购，历史订单仍可查询。旧 `18000 → 18083` 服务停止，源库保留。
合并与回退边界见 [2026-09-09 合并记录](../docs/acceptance/merged-commerce-2026-09-09.md)。

本机已有 Node 24。PowerShell：

```powershell
cd F:\agent\frontend
npm.cmd ci
npm.cmd run dev
```

打开 http://127.0.0.1:5173/。开发服务只监听回环地址，端口占用会报错，不会悄悄更换地址。

- 统一首页 `/`：游客聊天和查看商品；登录后收藏、预览/确认订单、预览/确认支付、查看自己的订单。需要现有 MySQL、Redis、Java、检索、Agent/BFF 服务及 `COMMERCE_DEMO_ENABLED=true`、`AGENT_TRANSACTION_ENABLED=true`。
- 旧订单页 `/legacy-orders`；旧静态示例 `/?demo=1` 保留 26 笔标注的样例，不关联账户、不生成订单，接口失败不会自动切示例。
- 注册将真正创建本地账户，下单会写入本地数据库。只有已有 verified 价格且配置了有限本地库存的二手手机允许交易；不把库存当作真实商家证据。初始化脚本为 `scripts/commerce-workspace-stock-20260908.sql`，不覆盖价格、不补满已有库存。
- 本地模拟支付需同时开启 BFF `COMMERCE_DEMO_PAYMENT_SIMULATION_ENABLED=true` 与 Java `PAYMENT_SIMULATOR_ENABLED=true`；按钮明确标注本地模拟，不扣款。不要对生产环境运行测试。
- 原 `/commerce-demo`、`/agent-flow` 入口保留，通过现有代理访问；旧独立 18000 演示不是统一前端的依赖。
- 统一用同一个主机名访问；localhost 与 127.0.0.1 的 Cookie 不通用。后端开机初次启动先等 Java readiness 成功，再启动 Agent，避免旧有评论索引启动读取失败。

## 功能

- 橙色电商视觉、聊天为主体，商品卡片随回复展示，交易确认按需展开。
- 新订单采用 Java 多商品分摊模型（界面仍一次选择一种商品），支持取消、支付倒计时、按数量退款预览/确认和真实模拟仓库状态；已开始派发则拒绝自动退款。
- Java BIGINT 商品标识超过 JavaScript 安全整数时以字符串传输，不四舍五入。

- 对话流式展示、历史恢复、历史商品与本人收藏重新校验价格/库存后选择；单商品数量 1–20，明确确认卡。
- 下单超时保留原确认卡，只回查原结果；未确认结果时禁止新建交易。令牌刷新后仍可回查同一所有者的浏览器确认记录。
- 收藏只表示保存商品，不自动当成推荐正反馈或长期偏好。
- 当前保留最近 60 条对话（14 天 TTL）与最多 200 项收藏列表；登录过期后重新登录，没有自动令牌续期。
- AI 回答支持只读 Markdown 表格和列表，不执行 HTML，不加载模型给出的图片，不把模型链接当作支付入口。
- 注册、登录、刷新页面恢复会话、退出；HttpOnly Cookie，JWT/Refresh Token 留在服务端，浏览器只持有内存中的 CSRF。
- 多商品订单、人民币分值格式化、七种订单状态、金额分解、按需读取最新详情、复制订单号。
- 默认每批 20 单；原样携带 nextCursor；按 id 合并去重；切状态清空游标；搜索明确只在“已加载订单”中进行。
- 请求取消、过期响应隔离、重复点击锁；加载/空数据/错误/重试、401与跨页面会话更新处理、429 Retry-After 文本提示。
- 桌面与移动布局、键盘焦点、原生对话框与 Escape、减少动画偏好。不把已加载数量描述为数据库总订单数。

## API 与安全边界

浏览器 → Vite/Nginx 同源代理 → commerce_demo.py → Java 订单权限与分页 → MySQL。

统一首页还使用 `commerce_workspace.py`：服务器生成不公开的访客/会话标识，浏览器不能传 owner、engine、task 或候选范围。普通聊天不携带交易 JWT；点击卡片先验证来自本人历史或收藏，再建立独立的服务端人工选择上下文，重新请求 Java 预览并生成确认卡。此路径不伪造或修改 Agent 的 CandidateScope；旧 TransactionAgent API 的候选校验保持不变。

浏览器确认命令在每次操作时重新认证所有者，绑定服务端会话和任务；不因 Access Token 刷新产生另一条命令。非浏览器的既有 Agent 凭据绑定规则不变。Redis 回执消失不能证明数据库未提交，未知结果继续保持待核实，不能盲目新下单。

GET /api/commerce-demo/orders/page?size=20&status=PAID&cursor=... 返回 {orders,nextCursor,hasMore}；GET /api/commerce-demo/orders/{id} 返回 OrderResponse。新 BFF 只剥离 Java ApiResponse 外层，不改变分页算法或事务。

两个订单 GET 也发送 X-CSRF-Token：这里除了防跨站，还用于绑定“此标签页缓存的身份”与“浏览器当前 Cookie 会话”。另一个标签页登录其他人或调用 /me 轮换 CSRF 后，旧标签页会被拒绝并清空，而非在旧用户名下显示另一人的订单。

不要把 Java JWT 放到 localStorage，不关闭现有同源校验；不要把代理 changeOrigin 改为 true 而忽略 Host/Origin 配对。COMMERCE_BFF_URL 仅用于开发服务器代理目标，不是浏览器凭据；参考 .env.example。

## 验证

```powershell
npm.cmd run build
npm.cmd run lint
npm.cmd test
npm.cmd run test:e2e
```

浏览器测试默认使用已安装的 Chrome。无 Chrome 时安装 Playwright Chromium，并在 playwright.config.ts 配置相应 channel；可用 PW_BROWSER_CHANNEL=msedge 切换本机 Edge。

普通 E2E 用请求拦截模拟服务，仅测试浏览器行为，不冒充真实 MySQL 验收。独立的 live.spec.ts 默认跳过；开启方式：

```powershell
$env:RUN_LIVE_COMMERCE='1'
npm.cmd run test:e2e -- tests/live.spec.ts
```

该测试实际创建一个 frontend-qa-* 本地测试账户并执行注册、登录、会话恢复、空订单查询、退出；不下单、不支付，不记录密码与 Cookie，不自动删除数据库用户。不要在生产环境执行。

统一版完整本地交易测试（会新增隔离账号、收藏、订单和模拟支付）：

```powershell
$env:RUN_LIVE_SHOP='1'
npm.cmd run test:e2e -- tests/shop-live.spec.ts
```

该测试不会删除测试账号或订单。导购请求可能命中确定性快速路径；仅 HTTP/SSE 成功不能证明实际调用了模型，真实模型调用另查对应 trace。失败时的 Playwright trace 可能含登录表单，不能作为公开简历附件。

`shop-live-safety.spec.ts` 另验真实跨账户和跨标签页隔离，不创建订单；需显式提供前一轮隔离测试的 `LIVE_FOREIGN_ORDER_ID` 与 `LIVE_FOREIGN_PAYMENT_ID`。默认跳过，不能使用生产订单标识。

最终局部验证：Python 75、Vitest 22、模拟浏览器 23；真实闭环和真实安全用例各通过。模型真实调用使用独立 SDK 元数据观察器确认，不伪装为浏览器捕获；详细证据和失败记录见验收文档。

## 构建与部署

npm run build 生成 dist；npm run preview 仅用于本机预览，不作为生产服务器。已附 Dockerfile/nginx.conf/compose.yaml，供部署复用：从 F:\agent 根目录执行 docker compose -f docker-compose.yml -f frontend/compose.yaml up -d --build --no-deps frontend。先关闭占用 5173 的开发服务，且现有 Agent 必须运行；Compose override 的 context 按首个 Compose 文件目录解析。

容器模板与 HTTPS 部署需按目标环境另验；不能因为本机开发验证通过就宣称生产上线。若启用 HTTPS，须正确配置代理信任、原始 scheme 和 Secure Cookie，不能绕过 BFF 同源检查。

## 以后按这个顺序学习

统一首页从 `src/Shop.tsx` 的状态、组件和事件开始，再看 `src/lib/workspace.ts` 的运行时数据验证与 SSE；不需要先重写后端。

1. src/lib/orders.ts：Java 返回的订单、明细、分页数据怎样映射为 TypeScript 类型，为什么还要运行时校验。
2. src/App.tsx 的 ItemRow/OrderCard：数据怎样变成可复用组件；先尝试改一个字段或布局。
3. src/hooks/useOrders.ts：首次读取、继续加载、筛选重置、旧响应丢弃；游标只是 opaque 参数，不是前端自己计算的页码。
4. src/lib/api.ts 与 AuthDialog：错误、会话、CSRF 和请求超时。
5. tests/orders.spec.ts：用一个失败场景复现，再尝试修改和补测试。

本轮为 AI 辅助实现。可以描述已交付的实现事实，但“独立完成/熟练掌握/生产性能”等个人能力与结果须由后续学习和证据另行验证。
