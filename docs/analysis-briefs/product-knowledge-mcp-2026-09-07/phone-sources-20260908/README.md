# 手机真实问题：型号与外部证据核查（2026-09-08）

本轮完成来源可行性审计。10条 = 7条正向知识需求 + 3条工程对照，**不是10条知识库可解决的失败**。
已重新核验21轮原文与原数据库行/会话/请求指纹；表中为历史开发案例，不是当前系统失败率。未运行模型、检索实验或服务。

## 逐条问题—商品—缺口—来源

7条知识需求中：5条有实际关联商品及部分外部资料，2条没有可用候选；没有做端到端回答实验，不能据此报成功率。

### 1. 学生用二手手机 要便宜点的  主要是打游戏

- 原记录：`rumr-v1-c001-t01`；原始佐证（本地材料：`../../../../agent/evaluation/assets/used_phone_harness_behavior_v1_20260825/public/scenarios.jsonl`）。
- 当时发生：已有确认的学生/便宜/游戏原文；本轮未取得可核验的原候选或完整诊断。
- 缺口与处理：先明确预算、游戏和画质目标，再绑定真实候选；不替该轮编造型号。
- 可用来源：本条不指定型号知识来源；原因见上。

### 2. 这里面那个适合打游戏 续航好？

- 原记录：`rumr-v1-c002-t03`；[原始佐证](../../../../data/annotations/ecommerce/used_phone_human_qrel_v2/intake/real_multiturn_session_001_transcript.md)。
- 当时发生：这里面原指两件Mate 40 Pro与一件畅享20；历史回复重新返回vivo Y35/Y3s/Y52s。
- 缺口与处理：已有Mate40Pro网页续航事实可复用；畅享20同协议游戏/续航实测未核验。还必须保留原候选与2000预算。
- 商品 `1795901`：华为Huawei/华为 Mate 40 Pro 5G官方正品有512内存；目录品牌：华为/HUAWEI。
- 商品 `7441112016001245431`：华为mate40pro 5g手机8+256g麒麟9000芯片4g全网通鸿蒙智能二手；目录品牌：华为/huawei。
- 商品 `1597799`：华为手机畅享20 全网通5G双卡双待5000毫安大电池 学生游戏手机；目录品牌：华为/HUAWEI。
- 可用来源：[S08 Notebookcheck Mate 40 Pro 原始评测](https://www.notebookcheck.net/Huawei-Mate-40-Pro-review-Top-smartphone-with-handicap.505829.0.html)

### 3. 主要用来拍照 不怎么打游戏

- 原记录：`rumr-v1-c004-t02`；[原始佐证](../../../../data/annotations/ecommerce/used_phone_human_qrel_v2/intake/real_multiturn_session_002_transcript.md)。
- 当时发生：历史返回荣耀80、X40；预算1200丢失，拍照否定游戏被路由为gaming_title_claim。
- 缺口与处理：相机规格可补，但不能仅按像素评胜负；两条目录均写华为，需处理品牌/型号绑定冲突。
- 商品 `8542161000270987328`：清仓华为荣耀80 便宜5g双卡双待手机1.6亿超清相拍照游戏备用手机；目录品牌：华为/huawei。
- 商品 `320619`：二手荣耀honor X40曲面屏手和平精英5G 12+256G配置华为正品手机；目录品牌：华为/HUAWEI。
- 可用来源：[S05 荣耀80 官方帮助中心](https://www.honor.com/cn/shop/help/category-240.html)；[S06 荣耀X40 官方帮助中心](https://www.honor.com/cn/shop/help/category-238.html)

### 4. 续航好 其他主要方面都可以的手机 你先推荐我看看

- 原记录：`rumr-v1-c005-t03`；[原始佐证](../../../../data/annotations/ecommerce/used_phone_human_qrel_v2/intake/real_multiturn_session_003_transcript.md)。
- 当时发生：实际回复总执行超时；原逐字记录说明没有执行检索、没有展示候选。
- 缺口与处理：先解决运行与探索性推荐；续航知识需求存在，本轮没有可绑定型号。
- 可用来源：本条不指定型号知识来源；原因见上。

### 5. 续航好的优先

- 原记录：`rumr-v1-c007-t02`；[原始佐证](../../../../data/annotations/ecommerce/used_phone_human_qrel_v2/intake/real_web_session_android_battery_budget_avoid_2026-08-25.md)。
- 当时发生：续航被映射为电池健康偏好；返回A96及两件标题未给具体型号的安卓手机。
- 缺口与处理：A96可取得型号容量/芯片；另两件身份不足；健康百分比不能直接比较不同型号续航。
- 商品 `1710694`：OPPOA96智能安卓5G手机骁龙695 处理器大屏游戏美颜拍照二手手机；目录品牌：OPPO。
- 商品 `1276807`：二手大屏幕6.5寸手机安卓手机特价4G智能手机学生手机备用手机；目录品牌：ANDROID GO EDITION。
- 商品 `214508`：9新二手OPPO 智能5.0寸移动手机便宜特价安卓手机学生备用机手机；目录品牌：OPPO。
- 可用来源：[S01 OPPO A96 中国版参数](https://www.oppo.com/cn/smartphones/series-a/a96/specs/)

### 6. 续航好手机500

- 原记录：`rumr-v1-c008-t07`；[原始佐证](../../../../docs/chat-recovery/0825_RECOVERED_TRANSCRIPT_2026-08-26.md)。
- 当时发生：500预算未应用，实际合成参考价855/856/857；候选Y3s、A11、A2。
- 缺口与处理：Y3s/A2容量资料可取得；A11可靠规格本轮未完成核验；不能以同为5000mAh判定同续航。
- 商品 `6055970412849301893`：vivo y3s游戏机5000毫安大电池长续航学生机备用机y3s二手9新；目录品牌：vivo。
- 商品 `4194616`：OPPOA11,256G大内存5000大电池全网通，二手95新128g；目录品牌：OPPO。
- 商品 `2732031`：OPPO A2全网通双5G手机12+512G大内存大电池大音量金刚石耐用99新；目录品牌：OPPO。
- 可用来源：[S03 OPPO A2 中国版参数](https://www.oppo.com/cn/smartphones/series-a/a2/specs/)；[S04 vivo Y3s 中国版参数](https://www.vivo.com.cn/vivo/param/y3s)

### 7. 拍照好的手机呢？

- 原记录：`rumr-v1-c008-t08`；[原始佐证](../../../../docs/chat-recovery/0825_RECOVERED_TRANSCRIPT_2026-08-26.md)。
- 当时发生：仅标题相关性召回，返回iPhone7 WiFi版、Find X6、荣耀80；保留健康偏好但未保留500预算。
- 缺口与处理：可以增加镜头/变焦/防抖依据；WiFi版实物和荣耀品牌冲突需核验；不据此生成无条件拍照总排名。
- 商品 `2675122`：9新苹果iPhone7二手游戏机WiFi版有摄像头32g指纹7代128g备用学生；目录品牌：苹果/Apple。
- 商品 `4705189315400771947`：oppo findx6三主摄拍照智能哈苏影像学生智能工作低价二手手机；目录品牌：oppo。
- 商品 `8542161000270987328`：清仓华为荣耀80 便宜5g双卡双待手机1.6亿超清相拍照游戏备用手机；目录品牌：华为/huawei。
- 可用来源：[S02 OPPO Find X6 中国版参数](https://www.oppo.com/cn/smartphones/series-find-x/find-x6/specs/)；[S05 荣耀80 官方帮助中心](https://www.honor.com/cn/shop/help/category-240.html)；[S07 iPhone 7 技术规格](https://support.apple.com/zh-cn/111943)

### 8. 我预算是1200呀 你这咋出现这么多的两三千的手机？

- 原记录：`rumr-v1-c003-t02`；原始佐证（本地材料：`../../../../agent/evaluation/assets/used_phone_harness_behavior_v1_20260825/public/scenarios.jsonl`）。
- 当时发生：用户纠正1200预算，原事故材料显示预算没有保留。
- 缺口与处理：由约束提取、状态继承和价格过滤解决；不伪装成缺少知识。
- 可用来源：本条不指定型号知识来源；原因见上。

### 9. 第一个和第二个对比

- 原记录：`rumr-v1-c004-t03`；[原始佐证](../../../../data/annotations/ecommerce/used_phone_human_qrel_v2/intake/real_multiturn_session_002_transcript.md)。
- 当时发生：第一个第二个指荣耀80和X40；比较时requirements清空，以字段完整度替代需求取舍。
- 缺口与处理：先保留候选序号、预算和拍照用途；新增相机资料属于上面c004-t02，不重复算独立知识需求。
- 商品 `8542161000270987328`：清仓华为荣耀80 便宜5g双卡双待手机1.6亿超清相拍照游戏备用手机；目录品牌：华为/huawei。
- 商品 `320619`：二手荣耀honor X40曲面屏手和平精英5G 12+256G配置华为正品手机；目录品牌：华为/HUAWEI。
- 可用来源：本条不指定型号知识来源；原因见上。

### 10. 就是性价比高的 不怎么打游戏 也不要求拍照 其他方面质量好的

- 原记录：`rumr-v1-c005-t02`；[原始佐证](../../../../data/annotations/ecommerce/used_phone_human_qrel_v2/intake/real_multiturn_session_003_transcript.md)。
- 当时发生：不怎么打游戏、也不要求拍照，却被追问不想要哪个品牌。
- 缺口与处理：否定语义和澄清策略问题；不能通过关键词把它统计为游戏/相机需求。
- 可用来源：本条不指定型号知识来源；原因见上。

## 已核对的外部来源

| ID | 来源 | 实际可取得的事实 | 使用边界 |
|---|---|---|---|
| S01 | [OPPO A96 中国版参数](https://www.oppo.com/cn/smartphones/series-a/a96/specs/)，入网型号、芯片、电池 | PFUM10；骁龙695；电池典型4500mAh，额定4385mAh。 | 只有型号规格，不能保证这台二手机续航或游戏帧率。 |
| S02 | [OPPO Find X6 中国版参数](https://www.oppo.com/cn/smartphones/series-find-x/find-x6/specs/)，入网型号、摄像头 | PGFM10；广角、超广角、潜望长焦均为5000万像素；广角与潜望长焦支持OIS；照片最高3倍光学变焦。 | 不是Find X6 Pro；硬件功能不能直接推出所有场景成像胜出。 |
| S03 | [OPPO A2 中国版参数](https://www.oppo.com/cn/smartphones/series-a/a2/specs/)，入网型号、电池 | PJB110；电池典型5000mAh、额定4880mAh；33W充电。 | 容量与充电功率不能替代续航测试。 |
| S04 | [vivo Y3s 中国版参数](https://www.vivo.com.cn/vivo/param/y3s)，建议零售价中的型号、处理器、电池信息 | V1901A/V1901T；MT6765；电池典型5000mAh；5V/2A充电。 | 需确认地区与入网型号；不使用官网首发价作为二手实价。 |
| S05 | [荣耀80 官方帮助中心](https://www.honor.com/cn/shop/help/category-240.html)，荣耀80的参数（非80 Pro/SE） | 该型号后置摄像头为1.6亿、800万、200万像素；芯片骁龙782G。 | 必须限定荣耀80小节；像素不等于实拍质量，原目录品牌华为有绑定冲突。 |
| S06 | [荣耀X40 官方帮助中心](https://www.honor.com/cn/shop/help/category-238.html)，荣耀X40的参数 | 后置5000万+200万像素；骁龙695；电池典型5100mAh。 | 目录将荣耀写作华为，先审查身份；机况未知不能由官网补齐。 |
| S07 | [iPhone 7 技术规格](https://support.apple.com/zh-cn/111943)，摄像头、视频拍摄、注释 | 标准iPhone 7为1200万像素摄像头；视频支持4K 30fps；页面2倍光学变焦条目仅限7 Plus。 | 不能将标准机功能背书给卖家WiFi版实物；不能混入7 Plus能力。 |
| S08 | [Notebookcheck Mate 40 Pro 原始评测](https://www.notebookcheck.net/Huawei-Mate-40-Pro-review-Top-smartphone-with-handicap.505829.0.html)，Battery life / Battery Runtime；Games | WiFi网页测试为10小时9分钟；亮度150cd/m²，Huawei Browser 11。游戏表列PUBG Mobile 1.1.0平均39.8fps。 | 历史测试样机和软件；游戏画质设置本轮未完整核对，不用于当前游戏承诺；不能与缺少同协议数据的畅享20直接排名。 |

## 首批范围与未完成项

1. 建议先整理 Mate 40 Pro、OPPO A96、Find X6、A2、vivo Y3s 的型号证据卡；这些型号直接来自上面的真实候选。先展示按型号限定的事实，不自动证明卖家实物身份。
2. 荣耀80/X40先作为品牌冲突样例；无型号的两件安卓手机保持未知；iPhone7 WiFi版先查实物功能，不能照搬标准机。
3. 不需要先扩充439商品库。需要新增的是证据和明确的商品—型号绑定；先做可读审查材料，再决定知识服务接线。
4. 畅享20同协议续航/游戏评测、OPPO A11可靠规格，本轮未完成核验；不能说网上不存在。Mate40Pro中国官网规格地址读取失败，官方售后入口可找到，但未从中取得完整规格。
5. 荣耀80/X40规格页本次文本解析出现空字段，改用官方帮助中心对应型号小节核验；不能把网页200或标题存在当成事实采集完成。
6. 已有5条型号续航事实、22条解析切片位于 `evaluation/used-phone-model-facts-dev-v1/`；Mate40Pro的609分钟本次原网页复核一致，保留其非生产排序授权、实物身份未验证边界。
7. 商品价格均为历史Demo合成参考价，不代表当前报价。目录七项机况是来源字段的受控解释，不是本轮实物检测；官方资料不能验证某台二手机维修史。

## 核验文件

- cases.jsonl（本地材料：`cases.jsonl`）：逐字Query、原文哈希、来源、完整可用诊断回答、候选ID与分析。
- sources.jsonl（本地材料：`sources.jsonl`）：8个核对过的外部来源、事实摘要、适用边界；不是已部署知识库。
- catalog-excerpts.jsonl（本地材料：`catalog-excerpts.jsonl`）：13件相关商品的原行摘录，只是审计证据，不新增数据底座。
- summary.json（本地材料：`summary.json`）：来源文件哈希、21轮回连结果、原有事实关联。
- [审计脚本](../audit_phone_sources.py)：默认仅校验本地来源，指定新目录才写审计产物；不联网刷新来源、不跑实验。
