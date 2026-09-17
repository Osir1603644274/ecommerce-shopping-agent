# 真实多轮会话 002：近似预算、拍照用途与序号比较

## 来源与边界

- 记录状态：`SELECTED_REAL_MULTITURN_PENDING_REVIEW_NOT_QREL`
- session_id：`d8f38f02-8062-455f-8f7d-69dce84a2888`
- 采集日期：2026-08-25（Asia/Shanghai）
- 路径：正式 439 网页 `step_debug`
- 语义用户轮次 / HTTP 尝试：3 / 3
- Query、request_id、时间和 outcome：`.runtime/used-phone-demo-439/web-query-intake.sqlite3`
- 系统回复与调试状态：项目所有者提供的真实网页逐字记录
- `humanAuthored=true`；`answerDerived=false`；不是 qrel 或标准答案
- 会话发生在完整 diagnostic payload 稳定留存前；不得补造缺失状态。

## 轮次 1

- request_id：`req-a37271c73ba1`
- submitted_at：`2026-08-25T09:08:13.414812+00:00`（北京时间 17:08:13）
- outcome：`completed`
- message SHA-256：`d438d684ce8f572375f46e89cc77a78418c61691cec630fde1dda98ca4731227`

### 用户原文

> 预算1200左右，希望华为手机

### 系统实际行为

系统称检索到 20 个候选，但 TaskState 只保留硬条件 `brand=huawei`，没有预算：

- ¥3371，华为 Mate 60 Pro，ID `57729`
- ¥2534，华为 Mate 40 Pro，ID `1795901`
- ¥2079，华为 Mate 40 Pro，ID `7441112016001245431`

页面明确显示：

```text
requirements = [{key: brand, operator: eq, value: huawei, priority: hard}]
taskStateExtraction = deterministic_complete / complete_controlled_coverage
modelCalled = false
```

因此 `预算1200左右` 被静默丢失，三项决选全部明显高于用户预算。

## 轮次 2

- request_id：`req-e69316b12db6`
- submitted_at：`2026-08-25T09:09:11.065192+00:00`（北京时间 17:09:11）
- outcome：`completed`
- message SHA-256：`bda97260f86a0749b6dd84625893e637ab675bc4f7189b9ff651308a36ff7751`
- 本轮是在看到轮次 1 的实际高价候选后输入。

### 用户原文

> 主要用来拍照 不怎么打游戏

### 系统实际行为

系统检索到 2 个候选：

1. 荣耀80，模拟参考价 ¥1516，ID `8542161000270987328`；标题声称“1.6亿超清相”，受控属性较完整。
2. 荣耀 X40，模拟参考价 ¥1033，ID `320619`；七项受控属性均未知。

系统同时声明标题中的拍照/游戏只是卖家描述，并非已验证性能。TaskState 仍只有华为品牌要求；`taskStateExtraction=deterministic_text_claim_discovery / gaming_title_claim`，没有保存预算或拍照偏好。

## 轮次 3

- request_id：`req-6a5132bc4a64`
- submitted_at：`2026-08-25T09:10:27.890848+00:00`（北京时间 17:10:27）
- outcome：`completed`
- message SHA-256：`19b0f2d45ccd65a50eff92ace53d368b3b8c9c77d67348c9be2de75224ba661f`
- 本轮是在看到轮次 2 的两个真实候选后输入。

### 用户原文

> 第一个和第二个对比

### 系统实际回复

系统执行唯一 `compare_products`，但比较状态为：

```text
requirements = []
modelCalled = false
answerSource = deterministic.validated_renderer
```

它仅按“未知/冲突字段更少，再比较已知风险”推荐 ID `8542161000270987328`。这款价格 ¥1516，超过先前约 ¥1200 的预算；其拍照优势只来自卖家标题声明。ID `320619` 虽在预算内，但七项受控属性未知。系统没有根据用户预算与“主要拍照”需求给出有证据边界的取舍。

## 待审查真实观测（不是 gold）

1. `预算1200左右` 未进入 TaskState，coverage 却错误报告 complete。
2. 第二轮拍照用途没有成为可持续的用户决策条件。
3. 比较阶段清空 requirements，并用字段完整度替代用户需求进行推荐。
4. 大整数商品 ID 在浏览器决选卡中发生 JavaScript 精度舍入。
5. 当前数据没有可信相机性能字段；合理结果应说明证据不足，不能把标题宣传词提升为事实。

另有同日真实网页 session `e0329293-523b-436b-ad76-9d788a6f79ce` 复现相同预算问题，并产生追问“我预算是1200呀 你这咋出现这么多的两三千的手机？”。该两轮 session 作为补充事故证据保存于原始 intake，不单独计入三轮场景。
