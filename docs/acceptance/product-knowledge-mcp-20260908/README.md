# 商品知识库与 MCP 实施记录

状态：`BOUNDED_FUNCTIONAL_ACCEPT`。本地实验Demo已显式启用；通用开关仍默认关闭，知识覆盖和语义质量不作全面通过声明。以[最终验收与实际数据](RESULT.md)为准。

## 数据入口

- 最新版本：[phone-v2覆盖表](../../../datasets/knowledge/phone-v2/COVERAGE.md)；冻结清单（本地材料：`../../../datasets/knowledge/phone-v2/manifest.json`）。知识版本 `phone-v2-081e5e82a58e`。
- 439件商品逐条登记标题审核：CLEAR 288、CONFLICT 50、AMBIGUOUS 89、UNKNOWN 12；归并171个型号组。CLEAR只是标题声明明确，不是实物鉴定或人工金标；地区/代际未确认继续保留。
- 425条事实、111个来源、99/171型号有事实；171组均有调查记录。尚有72组没有可收录事实，不声称独立实测或地区版本全覆盖，也不把“未收录”写成“不具备”。
- 复用原5条独立续航事实及8个来源回执，补入官方规格和可核查测试。V1保持原字节，已标记历史版本；35处型号绑定修正见V2逐条审核记录。
- 商品库保持原439件，catalog SHA256：`725c5fe9209c0b278004c61d24dafab21593c128e679ea0a1ecf3ae4eb433d75`。机况仍来自原商品字段；模拟参考价不是网络实时价格。

## 已实现的运行链

购物公开菜单：`search_products`、`get_product_details`、`search_product_evidence`、`compare_products`。旧`rerank_products_in_scope`保留历史调用兼容；新范围比较复用compare，不再依据标题宣传词排名。

比较核验当前商品范围、任务版本、继承硬条件，再经MCP取得型号证据；父模型在现有预算内权衡。模型输入去掉卖家广告标题，显示保留权威商品标题。引用必须属于该商品可绑定型号；不允许外来ID，商品ID以精确字符串交给模型。

未知身份不能绑定型号事实；服务中断只保留商品事实与明确缺口，不联网兜底。无证据时不能凭“常理、型号定位”给性能排名。原候选卡片顺序不因推荐文字改变，建议标记原序号或完整商品ID。

检索：明确型号过滤、问题字段过滤、结构化事实、BM25/BGE、RRF融合。用户点名的型号不能被其他候选替换；芯片/相机参数问答与游戏/续航实测保持不同字段。公开型号查询和关联商品查询共用同一快照。

## MCP实证与启动

只读本机服务：`http://127.0.0.1:18791/mcp`；仅开放证据搜索和按证据ID读取。配置在项目Codex配置（本地材料：`../../../.codex/config.toml`），没有修改全局配置。

```powershell
$env:PYTHONPATH='F:\agent\agent'
.venv/Scripts/python.exe -B -m app.product_knowledge.server
```

- V2组件验收（本地材料：`mcp-v2-attempt001/result.json`）：真实BM25/BGE、8路并发、按ID一致性、停止与恢复。
- Codex真实查询（本地材料：`codex-v2-query-result.txt`）：两个工具返回同版本和证据 `pk:d64cb16f95d212da55844930`；1019分钟是特定条件下的iPhone13样机网页续航，不是卖家二手机保证。
- 购物客户端确实使用HTTP MCP，没有用内存函数冒充协议查询。

默认 `PRODUCT_KNOWLEDGE_ENABLED=false`。本地实验Demo以`-Knowledge`显式开启，地址http://127.0.0.1:18000/；使用同一个MCP URL，保留不带开关的旧模式回退。

## 实验口径与失败原件

真实回归输入为 `agent/evaluation/real_user_multiturn_replay_20260903_v7/conversations.jsonl`，8段会话共21轮。新增开发题（本地材料：`development-cases-v1.jsonl`）4段8轮由Codex编写，覆盖参数、多轮、比较、否定与缺口，不是未见benchmark。

- `live-smoke-attempt001..006`：定位Graph策略字段缺失、前一步引用未解析、重复证明材料造成ContextView超预算；保留所有失败原件。
- `real-21-attempt001` / `development-attempt001`在确认同类失败后停止，有stopped.json，不是完整验收。
- `real-21-attempt002`完整21轮中仍有上下文超预算；800输出Token被思考过程耗尽，导致正文为空并降级。没有把这些返回200的回答算效果通过。
- `real-21-attempt003`发现复杂条件超预算和未引用的性能推断；作为开发诊断保留，不是最终版本质量依据。
- 最终原件为`real-21-attempt007`与`development-attempt007`：21轮、8轮双臂完成；实际模型deepseek-v4-flash，45秒请求、10000 Token上下文、800最终输出Token，最终回答均显式关闭思考。源码SHA与真实模型回执已保存。
- 223项相关测试通过。知识模式真实21轮16轮带来源、3轮保守回退，中位延迟3.82秒；开发8轮4轮带来源、5轮保守回退。没有宣称总体质量提升率，具体Token和旧模式对照见RESULT。

## 学习与剩余门槛

[学习笔记](../../learning-notes/2026-09-07-PRODUCT-KNOWLEDGE-MCP.md)持续记录真实Query、错误根因、修复和数据。

本轮实现、协议验证和有界对照已收尾。后续补齐72型号事实与游戏/相机实测，改善具体型号召回、去重展示及逐句推断审查；目前只有8条续航实测事实，不能包装为全场景性能比较。未提交、推送、训练或扩充商品库。
