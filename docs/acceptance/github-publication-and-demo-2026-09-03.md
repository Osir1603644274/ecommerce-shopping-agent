# GitHub 发布与演示验收

- 日期：2026-09-03
- 远程仓库：<https://github.com/Osir1603644274/ecommerce-shopping-agent>
- 范围：公开允许列表快照、真实浏览器本地演示、远程归档复核
- 结论：`PUBLIC_REPOSITORY_AND_BROWSER_DEMO_ACCEPT`

## 浏览器链路

- 新建演示用户并完成真实 Agent 检索，返回 1 个服务端验证候选。
- 订单预览经过 CandidateScope 校验，随后使用精确短语确认下单和发起支付。
- 本地支付模拟回调返回 `SUCCESS`。
- Agent 最终从 Java/MySQL 回查订单为 `PAID`、应付 `387.96` 元。
- 全过程生成 7 张阶段截图和一份约 13 秒动图；页面内容无密钥。

## 发布边界

- 远程仓库只来自 `public-snapshot-v10`，不是受保护本地工作树。
- manifest 固定所有公开文件的相对路径、大小和 SHA-256。
- 发布前后均执行密钥模式、超限文件、manifest、仓库卫生、Markdown 链接和定向测试复核。
- 不包含 `.env`、外部原始数据、sealed 评测、运行日志、简历或本地工作包。
- 不宣称真实支付、生产容量、多机容灾或跨系统 Exactly-once。
