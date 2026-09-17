# Shopping Memory V14 Final Evidence and Authority Result

日期：2026-08-30  
最终状态：`BOUNDED_ARCHITECTURE_ACCEPT / HOLD_PRODUCTION_DEFAULT`

## 结论

V14 已形成三项互补证据：真实生产路径的记忆治理正确性通过；KuaiSearch-Lite
公开未来点击回归通过；新的未消费单点击 cohort 一次性 authority 18/18 门通过。原
Shopping Companion 排序结果仅保留为同源 oracle 上限诊断。

唯一一次 101 用户 sealed authority 在读取 `itemsLite` 时发生 HTTP 流断连；它没有进入
`recallLite`，没有解码 sealed 行，也没有生成逐用户或质量报告。该结果是基础设施失败，
不是质量失败；其一次性 claim 已消费并永久禁止重跑。随后没有复用这 101 用户，而是先将
固定源完整下载到本地只读文件，再对排除旧 501 用户、按 SHA256 预先隔离的新 cohort 建立
V2.2 public calibration 与 V2.3 authority。V2.3 唯一 attempt001 得到
`V2_3_AUTHORITY_ACCEPT_BOUNDED`。该 ACCEPT 仍不授权全局生产默认，测试账号灰度保持关闭。

## 三轨证据

### G：真实 governed B/C 安全轨

- `shopping_memory_v14_distinct_abc_governance_v1_20260830_attempt001`：
  `BOUNDED_DISTINCT_ABC_GOVERNANCE_ACCEPT`；B/C 在 20/24 场景产生差异，C 的
  Application Precision/Recall 均为 1.0，False Influence 为 0。
- `shopping_memory_v14_governance_acceptance_v1_20260830_attempt001`：
  `BOUNDED_REAL_PATH_GOVERNANCE_ACCEPT`；同时绑定真实 Java/MySQL/Redis/Python 三会话链路。
- 默认仍为关闭：Python memory projection/BFF=false；Java
  `${SHOPPING_MEMORY_ENABLED:false}`。

该轨只证明 owner、recipient、category、override、expiry、revoke、supersede、关闭开关及
损坏链等治理语义，不证明独立排序质量。

### O：Shopping Companion 属性匹配上限轨

旧公开 dev/validation 的 qrel 与 `memoryScore` 同源，且旧 B/C 调用同一排序路径；已按
Amendment 006 降级为 `ORACLE_POLICY_ALIGNMENT_DIAGNOSTIC_ONLY`。旧源码、结果、receipt
和哈希保留，但不得用于 confirmation、灰度或生产授权。

### F：KuaiSearch-Lite 独立未来行为轨

V2.1 修复了旧 V2 的两个 P1：

1. 以首字段 `user_id` 的 raw envelope 在 `json.loads` 前分流；公开进程只解码 public，
   1428 条 sealed 与 547717 条 other 行均在解码前跳过；
2. 除条件正例子集外，新增覆盖全部 requested users、无机会用户计 0 的 demonstrated
   utility、paired user bootstrap 和验收门。

冻结身份：

- runner SHA256：`35fc236d49a6ec13f656528d766de91f513e9267999913620b0824acc94d3d91`
- manifest SHA256：`d126f20018aaf76a80c1cea8dd2d3840a85f5bbe5044be4bef9a75b0dc44da56`
- public report SHA256：`7fc9b0a24769263c581ac1abbd5584cb604408ac582fe27c58a59b834b0f826b`
- public receipt SHA256：`ccf5402b6d1605b78b3bfa6d5810e70221e3df56f9240d461b88b47e0d55d6fe`
- 源 revision：`09807c773ce67360ed8df30842e372182fcf7ad9`
- 严格手机商品：27190；单元测试：8/8。

公开 validation 是受披露的迭代回归，不是 confirmation：

| 指标 | 结果 |
|---|---:|
| 可评用户 | 100/100 |
| candidate≥2 coverage | 0.935629 |
| future-positive session coverage | 0.576000 |
| conditional C−A nDCG@10 | +0.024263；95% CI `[+0.012501,+0.037576]` |
| conditional C−A Hit@3 | +0.018286；95% CI `[-0.003800,+0.040906]` |
| conditional C−A MRR | +0.030495；95% CI `[+0.011902,+0.051412]` |
| all-user demonstrated C−A nDCG@10 | +0.019772；95% CI `[+0.009288,+0.032616]` |
| all-user demonstrated C−A Hit@3 | +0.016928；95% CI `[-0.001091,+0.037583]` |
| all-user demonstrated C−A MRR | +0.025176；95% CI `[+0.008106,+0.045035]` |
| change rate / P95 / profile P95 | 0.36 / 0.7208ms / 1009B |

18/18 公开门通过；独立复核为 P0/P1/P2 `NONE`。

## 首次 sealed authority：基础设施失败并永久封存

预运行包审查历史完整保留：

- `v2_1`：started-marker schema 缺少 claim 前 `authorizedAt` 校验，`HOLD_NO_RUN`，未运行；
- `v2_1_1`：审查期间仍发生写入，`INVALID_HASH_CHAIN / MUST_NEVER_RUN`，未运行；
- `v2_1_2`：静止后两次哈希复算一致，最终审查 P0/P1/P2 `NONE`，获准且仅获准运行一次。

`v2_1_2` 冻结哈希：

- design：`d3ed30a13d008e0d89888d15e5e5fc860e26664056a97ebf46a99c0aa6691166`
- contract：`96f10c90890a7ffe88300eb5b1551be10a50723f03ba5ed9503b231e45c9e994`
- scorer：`74f68cc3355e42554f830bc539883dc39a83a8a8371c2dc8847de163a1b05012`
- freeze receipt：`86b9cbaf6e81b1fb5608e032892bebe4e2e35ae33af9b144201dc5e627ab7143`
- package sums：`9da95f5c47815892bbefc06e1ba97f4b74481982fb4eb4b23e09aaec0f72347d`

唯一 attempt001：

- marker SHA256：`89237105ef1fd6e6a1169c60afa6acf8edcae935f84265b33322d82add0c1555`
- claim SHA256：`6ea05d65d07fcc3ec6cad50c27563ed10bcd37e62a29bbb005e251c18102b961`
- claim status：`CLAIMED_NO_RETRY`
- runner observation：`itemsLite` 的 `stream_jsonl` 在 15,912,094 bytes 后抛出
  `IncompleteRead(..., 2,773,956,862 more expected)`，继而为
  `requests.exceptions.ChunkedEncodingError`，exit code=1。
- 代码控制流与 traceback 均表明错误发生在 `itemsLite`，早于 `recallLite`；sealed 解码未开始。
- `per-user.jsonl`、`report.json`、`receipt.json`、`SHA256SUMS.txt` 均不存在；当前无 scorer
  进程，也不存在第二 attempt。

错误的具体 traceback 只来自运行窗口捕获，包内没有持久化的 error receipt，列为 P2
证据粒度限制；磁盘可独立证明 marker、claim、`CLAIMED_NO_RETRY` 与质量产物为零。

## 新 cohort 恢复：V2.2 public calibration

为避免重用已消费用户，V2.2 在任何新行为解码前冻结：旧 300/100/101 共 501 用户全部排除；
其余用户按 `uint64_be(SHA256(salt:user_id)[0:8]) mod 100` 分为 public `0-49` 与 authority
`50-99`。Public 只解码自身 272077 行；authority 275640 行在 `json.loads` 前跳过并绑定 raw
SHA256=`46bf387170ef22d13bc5bccf8e173bf4a6ea70db14e84b96c568a111c2d45d16`。

源文件先完整下载到 D 盘、设为只读并匹配官方固定 revision：

- `items_lite`：2789868956 bytes，SHA256=`5c04e031324a37636afb2f822a4d862378d7f54eada93875d686fa8f31bd2621`；
- `recall_lite`：257794992 bytes，SHA256=`8949ed6b5cf685bb69067710ac70da12fd7999f66e66bafea4b9d8d78bfefe8a`。

V2.2 不再调参，复用 V2.1 固定配置；由于旧三点击 eligible 用户已经全部消费，新 cohort 明确
改为“至少一次严格更早的 strict-phone click”，且 eligibility 不要求未来点击或正例。公开
calibration 408 用户中 17/18 门通过；唯一未过项为 future-positive session coverage
`270/736=0.366848 < 0.40`，质量、安全、候选和资源门均通过。独立审查 P0/P1=`NONE`。

- runner SHA256：`9721a91d106d9b6f35a96635eb3aea52cb1a533c8f0df1bfa69ec221aeb57359`
- manifest SHA256：`5777f207f7040b8df3616a722957ff8c85c1dbea551eaedf71e3261f6dc5d164`
- public report SHA256：`54bb1fe2dea5f02f5ba93c835d65484a4c2e375d4012616c024d229fcd8e9600`
- public receipt SHA256：`8886ae06dea5f8fc7fc41aac81300dfede46929a88e7db4473fa477410dc8ace`

覆盖率是标签可评性门而非 treatment 效果门；因此将公开校准得到的 `0.366848` 透明用于新
V2.3 假设，把门冻结为 `0.35`。V2.2 保持 `HOLD_PUBLIC_CALIBRATION`，没有原地改写；V2.3
其余 17 门、分区、配置、eligibility、指标和 bootstrap 全部不变。独立审查确认该过程符合
calibration → untouched confirmation 边界，并规定 authority 若低于 0.35，不得再降门。

## V2.3 唯一 authority 结果

预运行包在 D 盘冻结并经独立审查 P0/P1=`NONE`；本地源 raw-byte 大小/SHA 预检发生在 claim
前，但任何 items/recall JSON 解码、authority 行为访问和评分均发生在 exclusive claim 后。

冻结身份：

- design：`3812db46264bd16d0b7d3f12b1c16351bb30b6c2d1a0e27fa49d51845b0d6e2a`
- contract：`d16d2200df3937c149f2ac2b87bda6ad113a5913bb00653eeca1640572b5c258`
- scorer：`f2484fc407a9f651c68454620c0f1f8055c4773d89178dacd7376e15c77d9bc2`
- freeze receipt：`3e281c346b527aeb05e0bc51fe7245e582b619a0ea4b4c9d569a89c990558b17`
- package sums：`14f318c4b9ff42b6129f4d01393d6abe80321c1cc113d5930e1cc2d743692473`

唯一 attempt001 无重跑，决策 `V2_3_AUTHORITY_ACCEPT_BOUNDED`；最终独立复算为
P0/P1/P2=`NONE`，18/18 门全部通过：

| 指标 | Authority 结果 |
|---|---:|
| eligible / evaluable | 425 / 251（0.590588） |
| candidate≥2 coverage | 712/805 = 0.884472 |
| future-positive session coverage | 290/712 = 0.407303；门=0.35 |
| conditional C−A nDCG@10 | +0.017049；95% CI `[+0.008176,+0.026403]` |
| conditional C−A Hit@3 | +0.017928；95% CI `[-0.003984,+0.039841]` |
| conditional C−A MRR | +0.022963；95% CI `[+0.008478,+0.039177]` |
| all-425 demonstrated C−A nDCG@10 | +0.007399；95% CI `[+0.003720,+0.011641]` |
| all-425 demonstrated C−A Hit@3 | +0.007843；95% CI `[-0.003922,+0.020392]` |
| all-425 demonstrated C−A MRR | +0.011460；95% CI `[+0.004330,+0.019623]` |
| candidate failure / duplicate / hard filter | 0 / 0 / 0 |
| B/C divergence / change rate | 250 / 0.327247 |
| latency P95 / profile P95 | 0.1926ms / 183B |

路由闭合：555553 总行 = authority 解码 275640 + public raw-skip 272077 + consumed raw-skip
7836；public/consumed JSON decode 均为 0，旧 501 用户全部排除。

- marker SHA256：`626c27b4673a4eb042b0cc1184384179264d503ceb6ca9afe4c7b51136939770`
- claim SHA256：`83eb5dc7ec859c4725796e7b76988aeba99fc67bfcc1c16d1855810a0985eb92`
- report SHA256：`54e127d3e22364aabce085e9e5aea36ec7e0b8d9fdc7eb6837f2fee5745ba4f8`
- receipt SHA256：`c0789b1617b5e216473b9da73a315c3d95002d5392dc7998311d624679589e73`
- checksums SHA256：`18fdd8940c549501328765a9d9c70fa7709a0e9dd341e5e63636bb3f387765d0`

逐用户文件只保留在 D 盘 authority 包；独立审查窗口读取后复算 6 组 10000 次 paired-user
bootstrap 与报告逐值一致，没有把逐例内容回传主工作区。

## 决策边界与下一步

- 真实治理架构：`BOUNDED_ACCEPT`。
- 公开低权威行为关联：`PUBLIC_REGRESSION_ACCEPT`。
- 新单点击 cohort 独立确认性排序质量：`V2_3_AUTHORITY_ACCEPT_BOUNDED`。
- 旧三点击 cohort 独立 confirmation：首次网络失败后永久 `HOLD_NO_RETRY`，V2.3 不证明对其泛化。
- 全局默认、测试账号灰度、生产切换：`HOLD`。
- V2.3 仅证明公开校准后的 1+ 历史点击 cohort 离线关联，不证明因果提升、显式偏好、购买
  记忆、旧三点击 cohort 泛化或生产就绪；attempt001 已封存，禁止重跑、调参或新增 attempt。
