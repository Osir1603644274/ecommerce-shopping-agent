# 数据资产全景盘点与学习记录（2026-09-08）

## 入口与完成内容

- [总入口](../../../datasets/README.md) → [30 个重点资产条目](../../../datasets/ASSETS.md) → [202 个完整来源组](../../../datasets/ALL_LOCATIONS.md)。
- assets.json（本地材料：`../../../datasets/assets.json`） 供机器读取：类别、规模、来源性质、生命周期、关系、文件位置/哈希、已有结果位置。
- snapshot002/files.jsonl（本地材料：`snapshot002/files.jsonl`）：13,726 个结构化候选文件逐项清单；解析 13,529 个，197 个仅元数据登记（大型文件、数据库、压缩包等），读取错误 0、解析错误 0。
- 扫描边界（本地材料：`snapshot002/coverage.json`）：15 个显式根目录及2个运行留存库、跳过目录、兼容联接和访问错误；不是对全电脑所有可能文件的无限完整性保证。
- 同字节文件（本地材料：`same-bytes.json`）：738 组，仅记录不去重、不删除；hash一致不能说明可以破坏冻结包。
- 验收结果（本地材料：`verification.json`）。本轮只读原数据，未搬迁、删除或修改数据内容，未运行模型或 benchmark。

## 纠正了什么遗漏

上轮整理主要围绕商品库与24 Query，遗漏了多轮输入的具体位置。这次将行为、任务状态、ReAct、Mission、长对话、跨会话记忆和旧开发集独立列出。同时补上D盘KuaiSearch Lite行为原件、Shopping Companion独立来源、跨盘Context和记忆实验。

第一轮扫描仅按dataset/query等文件名关键词发现2,022个候选，但漏掉了`dev.jsonl`、`items_lite.train.jsonl`这类重要数据。已改为扫描范围内所有JSON/JSONL/Parquet/CSV/SQLite等结构化文件，另捕获Python数据定义；第二轮增至13,726个。旧snapshot001保留为方法修正记录，不是当前总账。

另修正两类格式识别：Yelp `.json` 实际为逐行JSON；两份北京CSV不是UTF-8。读取器支持逐行回退与GB18030，未改写原件。

## 来源关系的客观核对

1. 真人回放8会话21轮，与行为24场景65轮逐条比较完整用户文本序列：**6会话16轮完全匹配**；2会话不完全匹配，不能把整套数据说成同一子集。证据：relationships.json（本地材料：`relationships.json`）。
2. TaskState/Context AB v6的manifest和selection直接指向行为24场景65轮；它是复用实验，不增加独立场景数。
3. F盘vivo48脚本与E盘ABC冻结脚本SHA256完全相同。A/B/C是同输入的三组运行，不是三个新数据集。
4. 通用、vivo、苹果长对话保留familyId与KuaiSearch原Query ID；同family的修订和扩写版本不当独立来源。
5. 24条检索、24场景65轮行为集、24场景33轮架构集、24条记忆case是四种不同材料，不能依赖相同数字判断身份。
6. 记忆自然候选输入定义在冻结的dataset.py和trajectories.py；不能因为没有单独cases.jsonl就判定没有数据。规模引用原报告，本次没有执行生成器。

## 如何防止再次遗漏

```powershell
python datasets/verify.py
python datasets/catalog.py
```

verify检查30个重点条目文件位置、大小、可用哈希及此前迁移原件；大型源文件不强制重读数GB计算哈希。catalog以固定根目录重新枚举，对照已审阅snapshot002，报告新增、移除、元数据变化的候选。它不自动认定新增文件为独立数据集，也不自动更新快照。

新增材料处理顺序：发现差异 → 检查来源/角色/重复关系 → 更新重点账或配套候选定位 → 新建快照 → 同步入口。已有快照和正式实验原件不覆盖。日期不等于完成状态，文件名latest/current不等于实际权威。

## 边界

完整位置表包含候选与配套文件，不表示202个组都已通过人工标注审查或有202个独立benchmark。部分旧模块与结果只标为配套/历史候选，未提升为当前数据。

固定排除source_snapshot、baseline、pytest临时目录、状态日志、模型逐调用记录等执行副本；所有跳过路径都写在coverage中，原件仍在磁盘。当前盘点覆盖的是已知F/D/E项目数据根，不扫描与项目无关的个人目录；后续发现新数据根须登记并扩展扫描范围。
