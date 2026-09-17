# React / TypeScript 我的订单页：本地交付验收

日期：2026-09-08。用户因投递时间紧，明确将“引导练习”改为“先实际完成，之后再学”。本轮为 AI 辅助工程实现，不是学习者独立掌握证据。

## 交付范围

- 在用户已创建的 Vite 模板上完成 `frontend/`：注册登录、会话恢复退出、多商品订单列表、七种状态筛选、游标继续加载、已加载数据搜索、按需最新详情、金额分解、移动布局和明确标识的只读示例。
- 不重写 Java 交易后端；在 `agent/app/api/commerce_demo.py` 增加两个只读订单 BFF 路由，复用当前用户身份及 Java 权限校验。
- JWT/Refresh Token 留在服务端，浏览器只用 HttpOnly Cookie 与内存 CSRF。两个订单 GET 也校验 CSRF，将标签页身份与当前 Cookie 会话绑定。
- 购物入口沿用现有 commerce-demo；本轮没有新增支付、取消、退款 UI，没有验证模型导购质量或执行订单写操作。
- 本机页面：http://127.0.0.1:5173/；无账户体验：http://127.0.0.1:5173/?demo=1 。示例不会写订单，API 故障不会自动降级为示例。

## 验证结果与边界

| 验证 | 结果 | 边界 |
|---|---|---|
| `npm.cmd run build` | TypeScript 与 Vite 生产构建通过 | 不等于线上部署 |
| `npm.cmd test` | 10 项通过 | DTO、游标、格式化、去重、会话与代理契约 |
| `npm.cmd run test:e2e` | 17 项通过，1 项默认跳过 | Chrome 浏览器；普通用例拦截 HTTP，使用模拟返回 |
| `RUN_LIVE_COMMERCE=1` 单独运行 `tests/live.spec.ts` | 1 项通过 | 真实浏览器 → Vite → BFF → Redis / Java / MySQL；注册、登录、刷新恢复、空订单、退出 |
| BFF pytest：`test_commerce_demo_orders.py` 与 `test_commerce_demo_api.py` | 37 项通过 | BFF 契约、身份、参数、安全与原演示 API 回归；不是完整后端测试套件 |
| `npm.cmd run lint` | 退出码 0，无错误，4 条警告 | Effect 状态同步及 ref 清理相关建议；未宣称零警告 |
| Compose override `config --services` | 通过 | Dockerfile/Nginx 模板未构建部署，HTTPS 未验收 |
| 浏览器视觉检查 | 桌面、手机详情、注册弹窗已查看 | 固定截图视口；不是全设备兼容性认证 |

真实联调仅创建一个 `frontend-qa-*` 本地测试账户，测试结束已退出；账户保留在数据库，未自动删除。不记录密码、Cookie 或 Token。没有下单、支付或调用模型。真实非空订单的详情、跨页读取尚未跑数据库端到端验收，由模拟浏览器与 BFF 契约测试覆盖，不能混为生产验收。

## 实际发现与修复

1. 旧购物页面除 BFF 外还请求三个既有端点：补齐 Vite、preview 与 Nginx 代理；保留原 Host，避免破坏同源校验。
2. 多标签页切换账户可能让旧用户名与新 Cookie 不匹配：订单读取绑定 CSRF，不匹配时清空身份和私有数据。
3. 退出期间切换示例可能留下旧私有 UI：锁定切换并在退出最终阶段再次清理。
4. 浏览器测试发现详情弹窗关闭后焦点未回到入口：增加显式焦点恢复，Escape / 键盘测试通过。
5. 示例数据的 TypeScript 字面量类型过窄导致初次编译失败：按真实 `Order['items']` 类型声明后构建通过。
6. Agent 首次启动在 Java 就绪前读取旧评论接口失败：等待 Java readiness，再启动 Agent；没有改动旧业务启动逻辑。

独立静态复审结论：P0 NONE、P1 NONE、P2 阻断项 NONE。该结论限本次改动与所列测试，不代表全仓或生产环境无缺陷。

## 本机运行与维护

本轮启动现有 MySQL、Redis、Kafka、Elasticsearch、Java backend、Agent 容器，末次检查均 healthy；未重建这些容器或删除卷。前端开发服务保留运行在 127.0.0.1:5173。未提交或推送代码，保留原脏工作区改动。

重启命令、部署模板和后续学习顺序见 [前端 README](../../frontend/README.md)。建议之后从 `src/lib/orders.ts` 与 `App.tsx` 的 `ItemRow/OrderCard` 开始，不把本次交付直接写成“独立熟练完成全栈”。

## 保存的证据

- 浏览器测试 JSON（本地材料：`frontend-orders-2026-09-08/results.json`）：17 expected、0 unexpected、0 flaky、1 skipped；真实联调为另一次 opt-in 执行，结果单列在上表。
- [桌面订单截图](frontend-orders-2026-09-08/desktop-orders.png)
- [手机详情截图](frontend-orders-2026-09-08/mobile-orders.png)
- [注册弹窗截图](frontend-orders-2026-09-08/registration-dialog.png)

关键源码 SHA-256（交付快照，后续编辑需重新计算）：

| 文件 | SHA-256 |
|---|---|
| `frontend/src/App.tsx` | `D37C08E2A1D57F03C30BB86292C0D9DABAFBD220F026B2F44E0A00BD5DFC8C0F` |
| `frontend/src/hooks/useOrders.ts` | `351C9E2A206C4EC2E0F2600183C6A19D0ECB41024F9242B157AECCB61CF84070` |
| `frontend/package-lock.json` | `2A0A49AAE6A72B614CF5BE66FC306973267E4354479E49E4A8D07EDAFCEA367B` |
| `agent/app/api/commerce_demo.py` | `10A7F69503D300750BC693C5B7138DB0C63383E27FCC3AF46670705B7ED218FA` |
| `agent/tests/test_commerce_demo_orders.py` | `E8EB96197153B5B5B4314C5155076D86264D65B71084799C0D690EC68078995E` |
