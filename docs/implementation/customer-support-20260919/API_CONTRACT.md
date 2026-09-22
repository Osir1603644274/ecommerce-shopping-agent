# 客服后端当前接口合同

浏览器BFF前缀为 `/api/commerce-demo/workspace/support`，要求先恢复购物空间、登录、同源请求和 X-CSRF-Token。资源路径包括 `/orders/{id}/cases`、`/cases/{id}`、`/cases/{id}/events`、`/preview`、`/confirm`、`/cases/{id}/cancel`、`/cases/{id}/return-shipment`、`/cases/{id}/wait-stock`、`/cases/{id}/conversion-preview`、`/conversion-confirm`、`/tickets`、`/tickets/{id}/events`、`/tickets/{id}/reply`。办理写入传 Idempotency-Key；预览不用。管理员模拟器不通过这些浏览器客服路由转发。

本文件描述已实现接口，不能替代运行验收。前端、RAG、工单和完整换货仍以 STATUS.md 为准。

## 开关

- `local-life.support.enabled=false`：默认禁止新申请、撤销及提交退货物流。
- `local-life.support.simulator-enabled=false`：默认禁止生成模拟审核、验收、退款回执。
- `local-life.support.recovery-enabled=false`：默认不启动自动回执回查；显式开启后每三秒扫描二十条到期回执。
- 关闭新申请后仍可查询和应用已存在的权威回执，避免遗留在途请求。

## 用户接口（需要登录，资源必须属于本人）

| 方法与路径 | 作用 |
|---|---|
| POST `/api/after-sales/preview` | 输入 orderId/itemId/quantity/type/reason，返回五分钟预览。新预览使同订单旧未用确认卡失效。 |
| POST `/api/after-sales/confirm` | 输入 previewId 及 Idempotency-Key；重新检查订单事实并持久占用数量/金额。 |
| GET `/api/after-sales/orders/{orderId}` | 查询本人订单最新一百笔售后；完整分页待前端阶段扩展。 |
| GET `/api/after-sales/{id}` | 查询售后阶段，不将申请成功等同退款到账。 |
| GET `/api/after-sales/{id}/events` | 查询办理与回执应用历史。 |
| POST `/api/after-sales/{id}/cancel` | 输入 expectedVersion 及 Idempotency-Key；仅允许待审核或未寄回阶段。 |
| POST `/api/after-sales/{id}/return-shipment` | 输入 expectedVersion/trackingNo 及 Idempotency-Key；只登记寄回，不证明仓库已收到。 |
| POST `/api/after-sales/reconcile` | 输入 receiptId；应用已存在且属于本人售后的回执，不能自行生成审核结论。 |
| POST `/api/after-sales/{id}/wait-stock` | 缺货选择继续等待，输入 expectedVersion 与 Idempotency-Key；不创建退款命令。 |
| POST `/api/after-sales/{id}/conversion-preview` | 验收后、尚未预占成功的换货生成五分钟转退款确认卡；展示原实付分摊金额。 |
| POST `/api/after-sales/conversion-confirm` | 输入 previewId 与 Idempotency-Key；检查归属、期限及售后版本，确认后才转退货退款并创建退款命令。 |

type 取 REFUND_ONLY、RETURN_REFUND、EXCHANGE。换货目标只能来自原商品/规格快照。
确认卡重放使用原键返回原售后；同键改预览冲突；不同键重复使用已确认卡冲突。
转退款卡同样支持原键重放，新卡使旧未使用卡失效。转退款不重开七天申请窗口，不重复验收或回库；实际到账仍需独立渠道回执。

## 模拟管理接口（必须管理员，不能暴露给客服模型）

| 方法与路径 | 作用 |
|---|---|
| POST `/api/admin/support-simulator/cases/{id}/receipts` | 写入审核/收货/验收回执，不在同一事务内直接改订单。 |
| POST `/api/admin/support-simulator/cases/{id}/refund-success` | 从现存退款命令复制支付身份、金额、币种及摘要，生成持久渠道回执。 |
| POST `/api/admin/support-simulator/cases/{id}/replacement-dispatch` | 输入 trackingNo 与 Idempotency-Key；从已预占补发快照生成零收费出库回执。 |
| POST `/api/admin/support-simulator/cases/{id}/replacement-received` | 输入原补发 trackingNo 与 Idempotency-Key；仅已出库记录可生成签收回执。 |
| POST `/api/admin/support-simulator/receipts/{receiptId}/apply` | 对明确回执执行独立业务事务。 |
| POST `/api/admin/support-simulator/cases/{id}/resume-review` | expectedVersion、ticketId及Idempotency-Key；关联已解决的本单核实/争议工单，仅恢复原仓库收货或验收检查。 |
| POST `/api/admin/support-simulator/orders/{id}/dispatch` | trackingNo 与 Idempotency-Key；复用原履约 claim/fence，独立记录原订单发货回执。 |
| POST `/api/admin/support-simulator/orders/{id}/received` | trackingNo 与 Idempotency-Key；仅匹配已发货单号才能记录签收回执。 |
| POST `/api/admin/support-simulator/order-receipts/{id}/apply` | 独立应用原订单物流回执；原键/原回执可重放。 |
| POST `/api/admin/support-simulator/orders/{id}/clock` | 为尚无物流回执和签收历史的订单绑定独立模拟时钟；重复绑定不重置。 |
| POST `/api/admin/support-simulator/orders/{id}/clock/advance` | expectedVersion、正整数 seconds 与 Idempotency-Key；只向前推进本订单时间，单次最多一年。 |

普通回执输入 event、expectedVersion、itemId、quantity、sellable、reason。
允许的 event 为 APPROVE、REJECT、RETURN_RECEIVED、INSPECTION_ACCEPTED、INSPECTION_DISPUTED。
收货与验收必须匹配商品和数量；匹配失败转 NEEDS_REVIEW 并保留原始回执。
可退款资格与可售状态分开：质量问题可验收批准退款、但库存处置必须为隔离。
原订单物流模拟还要求 `local-life.fulfillment.worker-enabled=false`，避免与自动仓库出库同时运行。支付不等于发货，记录签收回执也不等于业务已应用；客服查询仍读取原订单履约状态。原订单回执可由管理员显式回查或后台扫描应用，异常保留错误并延后三十秒。
模拟时间按订单隔离，用于独立物流回执时间、七天售后资格及申请/转退款确认卡TTL；签收业务记录采用回执发生时间，时间也绑定到回执摘要。尚未绑定的订单使用真实时间，不修改操作系统时间。已经确认的请求即使超过TTL，原键仍可回查既有结果。后台重试采用真实运行时间；控制台和固定场景脚本尚未接入。

## 工单接口

工单是独立沟通记录，不能替代审核、退款、出库或签收回执。写入受 `local-life.support.enabled` 控制。

| 方法与路径 | 作用 |
|---|---|
| POST `/api/support/tickets` | 本人 orderId、可选 caseId、category、summary 与 Idempotency-Key，创建或同键回查。关联售后必须属于同一订单。 |
| GET `/api/support/tickets?offset=0&limit=20` | 查询本人工单，limit 为1–100。 |
| GET `/api/support/tickets/{id}` | 本人工单状态与版本。 |
| GET `/api/support/tickets/{id}/events` | 本人工单沟通与处理记录。 |
| POST `/api/support/tickets/{id}/reply` | expectedVersion、message 与 Idempotency-Key，追加信息并回到 OPEN；已关闭工单拒绝追加。 |
| POST `/api/admin/support-simulator/tickets/{id}/actions` | 管理员输入 expectedVersion、action、message 与 Idempotency-Key；支持 REQUEST_INFO、RESOLVE、CLOSE。 |

类别为 DELIVERY_DELAY、PAYMENT_QUERY、AFTERSALE_DISPUTE、INFO_VERIFY、COMPLAINT。
OPEN 可要求补充资料成为 WAITING_CUSTOMER；这两种状态可处理为 RESOLVED，再关闭为 CLOSED。用户对尚未关闭的记录补充信息可重新进入 OPEN。所有改变保留审计事件；关闭不会释放售后占用、退款或修改库存。

## 恢复与金额边界

- 模拟回执先独立提交；接口响应丢失不删除回执。
- 业务处理锁定原订单，再核验回执摘要、预期售后版本、状态转换及支付金额。
- 同一回执重复应用不重复记账；不同回执不能越过售后版本或退款状态。
- 仅退款成功只更新退款分摊、命令、售后及事件，不恢复库存、不回退原履约状态。
- 退货验收先产生 RETURN_SELLABLE 或 RETURN_QUARANTINE 库存待办；待办 ACK 前不能完成退货退款账务。
- 回查任务每批先消费库存待办；本地库存写入与 ACK 同事务，独立库存经原持久命令日志投递，读到远端 ACK 后才能完成。失败库存待办记录错误和尝试次数，下一轮继续；尚未设置终止重试阈值。
- 可售退货将 sold 转回 available；隔离退货从流通库存 total/sold 扣除，数量保留在 inventory_return_receipt 隔离记录中。流通总量不包含隔离数量。
- returned_quantity 与旧 refunded_quantity 共用原购买数量上限；库存命令保持原标识重试，不能重复处置。
- 换货只在退货验收及库存处置 ACK 后创建独立 support_replacement 和库存预占。原订单库存记录不作为补发记录；商品、数量、规格从已确认售后快照复制，不接受模型另选规格。
- 本地预占与补发记录同事务；独立模式先持久投递 RESERVE 再等待 ACK。明确 insufficient_stock 才进入缺货选择，PENDING/REVIEW 不允许转退款。用户继续等待会使用新尝试标识，避免重放已经确定缺货的旧命令。
- 预占成功进入 REPLACEMENT_READY，只说明库存已占用。出库回执核验补发标识、商品/数量/规格、零收费及物流单号；库存 CONFIRM ACK 后才进入 REPLACEMENT_SHIPPED。独立库存命令未决时提交命令而保留回执 PENDING，后续继续回查。
- 签收回执须匹配已出库单号；成功后结束售后、释放活动占用。补发不创建新支付或修改原订单签收史。完成换出的数量从原订单可申请数量中扣除，补发商品后续争议由关联工单承接。
- 换货预占期限为订单业务时钟30天。到期后禁止新建补发出库回执；到期前已存在的独立出库回执阻止释放，原键仍可回查。无出库证据时进入REPLACEMENT_RELEASING，本地原子释放或等待远端RELEASE ACK；未知结果保持数量占用、禁止转退款。释放确认后进入WAITING_CHOICE，必须由用户重新选择等待或预览转退款，不自动退款。
- 自动回查对失败项记录 attempts/last_error/next_attempt_at，延后三十秒；不会把暂未成功的业务写成完成。
- 库存待办失败延后三十秒，远端未决延后三秒；补发恢复在售后记录持久保存尝试次数、错误和下次时间，优先处理尝试较少的到期项。
- 模拟器在原订单锁下限制同一阶段的不同键重复回执；退款成功、补发出库和签收分别只允许一个独立事件。冲突时必须回查原键，不创建第二份结果。
- 模拟退款成功只代表本地渠道回执，不代表真实银行或第三方支付。

换货超时、工单核实恢复和客服Agent工具/RAG编排已实现，验收范围以STATUS.md为准。后续必须补齐独立库存跨服务网络故障、真实模型与浏览器链路、240场景独立评测；现有定向测试不代表最终验收。

## 独立控制台补充

- 管理员 `GET /api/admin/support-simulator/cases/{id}` 返回指定售后、事件、回执状态和库存效果状态。
- 管理员 `POST /api/admin/support-simulator/cases/{id}/process` 推进该单既有退货库存效果及换货预占，不创建签收或到账回执。
- SupportSimulatorController所有入口同时要求ROLE_ADMIN及simulator-enabled；普通客服BFF无上述转发路由。
- 独立19093控制台与固定恢复脚本见scripts/customer_support/README.md。脚本不代用户确认申请或办理缺货转退款。

## 订单客服对话

- BFF `GET /api/commerce-demo/workspace/support/orders/{id}/conversation` 恢复本人所选订单的客服记录；未启用时返回enabled=false。
- 同路径POST接收message和16–64位requestId；登录、CSRF、同源、购物空间身份检查后读取Java本人订单，再进入每订单Redis写锁。同一requestId内容不可修改，已完成请求只回放，不重复模型或预览调用。
- 开关 `CUSTOMER_SUPPORT_AGENT_ENABLED` 默认false。启用前仍需隔离实链路与模型验收。前端5173订单详情中的客服区域在能力开启后显示。
- 模型仅输出受限规划：政策、订单/支付/原订单物流/换货补发状态查询、售后预览、工单草稿、澄清。数据查询及关键状态回答由服务端执行和格式化。原订单运单不作为补发进度。
- 模型不能确认申请、直接退款、写工单、制造模拟回执。返回的预览交给原确认卡；工单草稿通过用户显式点击后调用已有工单BFF。
- Redis记录每次尝试、模型原始usage/未知费用、失败和工具读取摘要；浏览器不接收模型诊断或服务端令牌。请求中断可原requestId回查重试。非流式JSON模式下firstContentMs记录可用答案完成时间，包含本次身份/订单读取等处理时间；HTTP传输耗时还须在外部runner测量。
- 商品问答已区分原订单规格/原单价与当前目录资料/快照价；仅检索所选SKU attributeText原文，保留来源和版本，不用推断属性替代原订单规格。
- 自然语言撤销/寄回/等待返回含caseId与expectedVersion的待确认草稿；转换退款只生成服务端预览。模型的case_number按本轮权威售后列表映射，执行前再按ID读取并校验归属与阶段。引用既有售后的失败重试重新规划，防止列表序号变化后沿用旧编号。
- 仍未完成真实模型质量、全部办理闭环实链路与240场景评测，不得将接口测试计为Agent成功率。


### 远端库存原命令重试（独立模拟器）

- `POST /api/admin/support-simulator/orders/{id}/inventory-retry`：请求体 `{"commandId":"已有命令编号"}`，命令必须属于指定原订单。TRY 使用原有订单提交核对流程；其余状态交给既有命令投递器。
- `POST /api/admin/support-simulator/cases/{id}/inventory-retry`：命令必须是该售后的退货库存 effect，或属于该售后的补发单。无关联返回404，不创建新命令。
- 两个接口只在远端库存模式注册，且要求管理员身份、模拟器开关开启。客服模型工具不暴露接口。
- 返回持久化的 command_id/order_id/status/attempts/last_error。HTTP成功只表示已完成本次重试；PENDING 不等于库存已确认，NEEDS_REVIEW 不自动改写为成功。库存变化和幂等性由原服务原命令控制。
- 独立脚本操作名：order-inventory-retry / inventory-retry；控制台提供相同操作。当前常驻18080镜像尚未更新，新接口不能据此宣称已经在常驻服务上线。
