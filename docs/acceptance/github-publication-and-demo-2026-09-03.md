# GitHub 发布与演示验收

- 日期：2026-09-03
- 远程仓库：<https://github.com/Osir1603644274/ecommerce-shopping-agent>
- 当前主分支与 `v1.0.1` Release 提交：`8925763510e64172ab057935defa0dd1d4c27f1a`
- Release：<https://github.com/Osir1603644274/ecommerce-shopping-agent/releases/tag/v1.0.1>
- 范围：公开允许列表快照、真实浏览器本地演示、远程归档复核
- 结论：`PUBLIC_REPOSITORY_AND_BROWSER_DEMO_ACCEPT`

## 浏览器链路

- 新建演示用户并完成真实 Agent 检索，返回 1 个服务端验证候选。
- 订单预览经过 CandidateScope 校验，随后使用精确短语确认下单和发起支付。
- 本地支付模拟回调返回 `SUCCESS`。
- Agent 最终从 Java/MySQL 回查订单为 `PAID`、应付 `387.96` 元。
- 全过程生成 7 张阶段截图和一份约 13 秒动图；页面内容无密钥。

## 发布边界

- 远程仓库只来自允许列表生成的 `public-snapshot-v*`，不是受保护本地工作树。
- 公开文本在生成 manifest 前统一为 UTF-8/LF，避免 Git 传输规范化造成发布后哈希漂移。
- manifest 固定所有公开文件的相对路径、大小和 SHA-256。
- 发布前后均执行密钥模式、超限文件、manifest、仓库卫生、Markdown 链接和定向测试复核。
- 不包含 `.env`、外部原始数据、sealed 评测、运行日志、简历或本地工作包。
- 不宣称真实支付、生产容量、多机容灾或跨系统 Exactly-once。

## 发布后复核

- v10 首次远程归档暴露 CRLF→LF 规范化导致的 manifest 哈希漂移；该失败未被算作通过。
- v11 在 manifest 生成前统一公开文本为 UTF-8/LF，并增加 `.gitattributes` 固定远程行为。
- 后续公开包继承同一规范，并增加只读 GitHub Actions 完整性检查。
- 从 GitHub 提交 `a8d1a567` 重新下载归档，得到 manifest 错配 `0`、密钥命中 `0`、超限文件 `0`。
- 远程归档仓库卫生和 8 个活动 Markdown 文档链接检查通过，定向测试 `72 passed`。
- 最终 v13 提交 `89257635` 再次下载复核，1037 个 manifest 资产仍为错配 `0`、密钥命中 `0`、超限文件 `0`；GitHub Actions run `33746399645` 通过。
- `v1.0.1` Release 附件包含同一 v13 manifest 和真实浏览器演示动图。
- GitHub 最终递归树为 1038 个 blob、153 个 tree，`truncated=false`；比 1037 个 manifest 资产多出的 1 个 blob 是 manifest 文件本身。
- `v1.0.1` manifest SHA-256 为 `b71ca21eca3b7dba0f73c76b9815ac138f582f14dac86a3ff772b6cf5aeb4082`，演示动图 SHA-256 为 `349294221407d9a97aeb4fec379cf0eab541eddbc011dc4f9425b0ca3a9ad8ec`，与本地重新计算一致。
- 常规 Git HTTPS 推送被本机不受信任证书链拒绝；未关闭 TLS 校验，改用已认证 GitHub CLI 的 REST Git Data API 写入同一快照。Chrome 匿名访问仓库返回 HTTP 200，README 与项目名均正常渲染。
