# 真人网页问题修复回放（非 qrel / 非 gold）

状态：`FUNCTIONAL_REPLAY_PASS`  
数据世界：439 条冻结二手手机快照；合成参考价仅用于预算和排序。

本文件保存用户真实提问触发的问题及自动化修复回放。原始真人消息仍以
`.runtime/used-phone-demo-439/web-query-intake.sqlite3` 为准；回放不冒充真人数据。

| 问题 | 原始证据 | 修复回放 | 结果 |
|---|---|---|---|
| 宽泛需求被反复追问 | `req-a1ff756f3293` / `req-bd3674ba0267` / `req-03cceb52283c` | `req-beb4f0ce5ef9` / `req-5221a23d4dd7` / `req-ba49efaca956` | 三轮均直接检索；“续航好”成为有序软偏好 |
| “苹果或安卓都可以”被写成矛盾硬条件且首轮很慢 | `req-b58abf2d952c` | `req-0691189bec1f` | 仅保留 `price_minor<=80000`；状态模型调用 0 次；3.19s |
| 1200 预算出现两三千商品 | `req-347ba6f68a53` / `req-c7cbc14c7216` | `req-491ef78cdc10` | 预算为 `120000 CNY_MINOR`，候选不越界 |
| “三千以内”从几百元开始 | `req-7e9abb03652e` | `req-a86512f6c0db` | 预算内按价格降序 |
| 追问当前三款却重新推荐新商品 | `req-fc1f88aabb4d` | `req-323ef8d2b044` | 只调用 `compare_products`，不调用 `search_products` |
| 国产手机混入苹果；续航追问重新搜索 | `req-40f5c45c85a9` 至 `req-aee93be112f7` | `req-9ed4c98435c9` / `req-87933027dcca` / `req-28c4ac888164` | 国产品牌组无苹果；第三轮只比较当前可见三款并给证据型建议 |
| 50 候选只能看到 3 个；大整数 ID 被浏览器取整 | 原网页复现 | `req-0691189bec1f` | `products=3`、`expandedProducts=20`；商品 ID 为 JSON string |
| 四轮约束累积丢失或顺序错误 | `req-612582023e78` 至 `req-2aac735cb6e3` | `req-4bf7dcd433c1` / `req-e467218aa02b` / `req-feb59768652d` / `req-a66f51f74c48` | Android、主板未维修、续航软偏好、预算与品牌排除均保留 |
| “续航好手机500”未生成价格约束 | `req-01f5af7afec2` / `req-e41c824e56a4` | `req-4888ac5bd895` | 识别省略“元/预算”的尾部金额；生成 `price_minor<=50000`，20 个候选最高 ¥476；活跃真人 TaskState 回填至 r18 |

临时架构策略：Validator 与 FinalAnswer 视图预算先放宽到 10000，Context 池化/投影优化留到
Harness 基线数据跑完后单独评估，避免当前真人功能修复被 Context 重构阻塞。

验证：`96` 个品牌/排序/Context 聚焦测试通过；`81` 个候选范围与可见比较测试通过；
439 启动器健康检查确认商品 439、ES 文档 439、检索通道 Elasticsearch。
