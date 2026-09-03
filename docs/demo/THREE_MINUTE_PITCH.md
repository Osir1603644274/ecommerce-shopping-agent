# 三分钟项目介绍

## 0:00—0:30：项目是什么

这是一个电商导购 Agent 与 Java 交易后端项目。Python/FastAPI 负责自然语言导购、检索、上下文与受约束 ReAct；Java/Spring Boot 负责身份、商品、库存、订单、支付和事件一致性。设计原则是：模型负责灵活决策，高风险写入必须回到确定性后端。

## 0:30—1:15：为什么是受约束 ReAct

早期固定 Plan-and-Execute 对预设流程清楚，但面对追问、比较和证据缺口不够灵活。项目最终采用 LangGraph 承载状态、Interrupt 和 Checkpoint，以 ReAct 决定下一步动作，再由 Harness 统一执行工具权限、Schema、预算、循环检测和失败回退。模型不能直接写订单，也不能伪造商品 ID。

## 1:15—2:00：检索和上下文

检索侧比较过 ES Standard、SmartCN、Dense、RRF、Cross-Encoder 和 LLM 重排，并使用 Recall、nDCG、硬约束违规与延迟共同选型。在线结果还会经过 MySQL 权威核验、TaskState 条件过滤和确定性重排。ContextPack、TaskState 与 ReferenceContext 分别解决预算投影、任务事实和“这个/第几个”指代绑定。

## 2:00—2:40：交易链路

用户选中服务端展示商品后，订单预览首先校验 CandidateScope；只有用户再次明确确认，TransactionAgent 才把请求交给 Java。Java 再检查 JWT、归属、库存、状态和幂等键。订单与 Outbox 同事务落库，Kafka 消费端以 Inbox 去重；支付回调后，Agent 重新查询 Java/MySQL 权威状态，而不是相信模型自己的历史文本。

## 2:40—3:00：结果与边界

当前本地 Demo 已真实完成注册、检索、下单、支付模拟回调和 `PAID` 回查，并保留服务端回执。项目没有宣称真实支付、跨系统 Exactly-once 或多机生产就绪；默认仍关闭交易与支付模拟开关，只通过专用脚本在本地演示时开启。
