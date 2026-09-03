# 开发与运行指南

## 环境要求

- Docker Desktop
- PowerShell 7（`pwsh`）
- Python 3.12
- Java 17
- DeepSeek API Key

完整 Demo 默认通过 Docker Compose 启动，因此不需要在宿主机单独安装 MySQL、Redis、Kafka 或 Elasticsearch。

## 配置

```powershell
Copy-Item .env.example .env
```

在 `.env` 中填写 `DEEPSEEK_API_KEY`。不要提交包含真实密钥的环境文件。

## 启动与停止

```powershell
pwsh -NoProfile -File .\scripts\commerce-demo.ps1 start
pwsh -NoProfile -File .\scripts\commerce-demo.ps1 health
pwsh -NoProfile -File .\scripts\commerce-demo.ps1 smoke
```

```powershell
pwsh -NoProfile -File .\scripts\commerce-demo.ps1 stop
```

`start` 会启动数据依赖、Java 后端和 FastAPI Agent；`health` 检查服务状态；`smoke` 执行一次注册、检索、下单、支付模拟和订单查询。

## 主要模块

- `agent/app`：Agent API、任务状态、上下文、ReAct Harness、工具和 TransactionAgent。
- `backend/src`：Java 模块化单体，包括身份、商品、库存、订单、支付和事件处理。
- `backend-gateway/src`：Spring Cloud Gateway 与服务拆分实验。
- `retrieval_judgment_pool_core`：多检索器候选池的确定性构建逻辑。
- `retrieval_judgment_pool_mcp`：候选池工具的本地 STDIO MCP 服务。

## 常用测试

```powershell
python -m pytest -q agent/tests/test_commerce_demo_api.py agent/tests/test_transaction_agent_api.py agent/tests/test_transaction_agent_runtime.py agent/tests/test_chat_endpoint.py
python scripts/check_repository_hygiene.py
python scripts/check_markdown_links.py
```

Java 服务的测试由 Maven 执行，Docker 镜像构建也会运行对应测试门。

## 常见问题

- **Docker Desktop 未启动**：先确认 Docker Engine 可用，再运行启动脚本。
- **端口占用**：检查 `8000`、`8080` 及中间件端口是否已被其他进程使用。
- **模型调用失败**：确认 `.env` 中的 API Key、模型地址和网络连接。
- **页面可以打开但交易按钮不可用**：请使用 `commerce-demo.ps1 start` 启动，脚本会加载本地交易演示配置。

