# 真人网页会话：安卓 + 未维修主板 + 续航偏好 + 预算 + 品牌否决

- 状态：`FIXED_WITH_LIVE_REPLAY`
- 真人会话：`d562f52e-1d6b-46dd-b192-86c8b3a08cee`
- 原始材料：用户网页实测粘贴记录
- 权威原始记录：`.runtime/used-phone-demo-439/web-query-intake.sqlite3`
- 数据边界：真人四轮只作为真实 query / failure intake；自动回放不是新真人数据、qrel 或 gold。

## 真人四轮与不可变诊断

| 轮次 | requestId | 用户原话 | diagnostic SHA-256 | bytes |
|---:|---|---|---|---:|
| 1 | `req-612582023e78` | 想要安卓、主板未维修的二手机 | `e8e8b6886d2d3ff7d58d4a05306ef205ef92953b1d2225bde5da0208672b0e6d` | 613755 |
| 2 | `req-25dff69d8355` | 续航好的优先 | `abe64f55b39bd43743e993a244fdc31650caf7ca0403beca7cd2e42e619e9fa7` | 895978 |
| 3 | `req-7a0517308ddb` | 预算3000以下 | `4731749c0b79f6b6ad11a37b4a1daf17389e6459ca229cae82ea8d5ca1f4611d` | 1201139 |
| 4 | `req-2aac735cb6e3` | 算了，不要华为或者三星的 | `7d5dc1a08eebd90541170bd555381b06d7a814544f6993c2608a11fbcf895855` | 1109727 |

四条记录均 `redaction_applied=0`；相同原话的 `message_sha256` 在回放中保持一致。

## 发现的故障

1. `battery_health in [90_plus, 80_90]` 只按“是否命中”计分，丢失列表顺序；80%–90% 可排在 90%+ 前。
2. `price_minor <= 300000` 只做资格过滤，未稳定表达“预算上限内从接近上限向下降序”。
3. 后端精确 ID `2237583271291033870`、`3343675802859811318` 作为 JSON number 进入浏览器后分别显示为 `2237583271291033900`、`3343675802859811300`。
4. 修复过程中首次回放发现当前 deterministic broad-discovery 分支会吞掉“续航好的优先”，导致该偏好完全未写入 TaskState；这是额外捕获的回归。

## 修复合同

- soft `in` 是有序偏好：首项权重最高；硬 `in` 仍只做集合成员判断。
- 排序优先级：硬条件已知满足 → 有序软偏好 → 硬预算上限内价格降序 → 固定评分 → productId。
- “续航好/续航优先/续航长”确定性映射为：
  `battery_health in [90_plus, 80_90]`, `priority=soft`，不调用模型、不宣称真实续航时长。
- 所有 browser-facing `guideResult.product.id` 均发布为字符串；内部执行与 Validator 仍使用整数身份。
- Validator 根据逐项 checks 重算 `softPreferenceScore`、预算排序价格和最终顺序，拒绝伪造分数。

## 修复后同句回放

- 回放 session：`codex-real-four-turn-fix-20260825-02`
- 环境：439 商品、439 Elasticsearch 文档、确定性非市场合成参考价。

| 轮次 | requestId | 结果 | 总耗时 |
|---:|---|---|---:|
| 1 | `req-4bf7dcd433c1` | Android + 主板未维修；大整数 ID 为精确字符串 | 2742.55 ms |
| 2 | `req-e467218aa02b` | deterministic 写入有序续航偏好；20/20 展开候选均为 `90_plus` | 3092.29 ms |
| 3 | `req-feb59768652d` | 20 个 `90_plus` 候选按价格 `277500 ... 131000` 降序 | 2786.46 ms |
| 4 | `req-a66f51f74c48` | 20 个候选无 canonical Huawei/Samsung；价格 `234400 ... 92000` 降序 | 3105.76 ms |

回放 diagnostic SHA-256：

- `req-4bf7dcd433c1`: `0ec20dcd9d02fa6c051c060d3b59be2cfc05db490850578c76a8cbf86f74ff64`
- `req-e467218aa02b`: `4081e93cdd6ced3d96024aa5677e1bebb3a2faee6076348e828403e3dd3c8e9f`
- `req-feb59768652d`: `d418ab0b399d7e7975407e358f0b2acee61e33730a5d652ff9c47c16684e033a`
- `req-a66f51f74c48`: `e31d02dba6e4cbe8608cbfe4d0ce609c189661c8a5c65f8128194b41569eac47`

说明：Honor 按当前 canonical brand 合同是独立品牌，不等同于 Huawei；本轮只执行用户明确否决的 Huawei 与 Samsung。

## 自动测试

- 排序、Validator、两阶段检索、网页 ID 投影与 provisional projection：87 passed。
- “电池质量好”与“续航好的优先”确定性解析：2 passed。

