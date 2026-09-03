# 全链路演示材料

本演示使用真实浏览器重新执行一次本地链路，不是静态模拟页面：

`注册 → Agent 检索 → CandidateScope → 订单预览 → 确认下单 → 支付预览 → 确认支付 → 本地回调 → Agent 回查 PAID`

![全链路动图](../assets/demo/commerce-demo-walkthrough.gif)

## 运行

```powershell
Copy-Item .env.example .env
# 仅在本机 .env 填写 DEEPSEEK_API_KEY
pwsh -NoProfile -File .\scripts\commerce-demo.ps1 start
pwsh -NoProfile -File .\scripts\commerce-demo.ps1 health
```

打开：

- `http://127.0.0.1:8000/commerce-demo`
- `http://127.0.0.1:8000/agent-flow`

结束后执行：

```powershell
pwsh -NoProfile -File .\scripts\commerce-demo.ps1 stop
```

## 本次浏览器证据

![Agent 检索形成服务端候选](../assets/demo/commerce-demo-search.png)

![Agent 回查权威支付状态](../assets/demo/commerce-demo-paid.png)

最终页面显示支付状态 `SUCCESS`，Agent 从 Java/MySQL 回查订单状态为 `PAID`。该结果只证明本地单实例、模拟支付回调链路，不代表真实支付或生产就绪。

口头介绍使用[三分钟项目讲稿](THREE_MINUTE_PITCH.md)。
