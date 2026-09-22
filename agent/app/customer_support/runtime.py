"""Owned facts and preview-only execution for the support semantic planner."""
from datetime import datetime, timezone
import hashlib
import json
import time
import uuid
from fastapi import HTTPException

from .knowledge import render_selected_policy

ORDER_STATES = {"PENDING_PAYMENT": "等待支付", "PAID": "已支付", "COMPLETED": "订单已完成", "CANCELLED": "订单已取消", "EXPIRED": "订单已过期", "REFUNDING": "退款处理中", "REFUNDED": "订单已退款"}
PAYMENT_STATES = {"CREATED": "支付单已创建，尚未支付成功", "PENDING": "等待支付", "SUCCESS": "支付成功", "FAILED": "支付失败", "CLOSED": "支付已关闭", "CANCELLED": "支付已取消", "REFUNDED": "已退款"}
LOGISTICS_STATES = {"WAITING_PAYMENT": "等待支付后履约", "READY": "系统尚未确认出库", "DISPATCHING": "出库处理中", "UNKNOWN": "出库结果尚待核实", "SHIPPED": "已出库，尚未签收", "RECEIVED": "已签收", "REFUND_HOLD": "退款处理中，暂停出库", "CANCELLED": "履约已取消", "NEEDS_REVIEW": "物流需人工核实"}
CASE_STATES = {"AWAITING_REVIEW": "等待审核", "AWAITING_RETURN": "等待登记寄回", "RETURN_IN_TRANSIT": "等待仓库收货", "AWAITING_INSPECTION": "等待验收", "REFUND_PENDING": "退款处理中，尚未确认到账", "WAITING_STOCK": "等待确认换货库存", "WAITING_CHOICE": "已确认缺货，等待你选择继续等待或预览转退款", "REPLACEMENT_READY": "已预占补发库存，系统尚未确认出库", "REPLACEMENT_SHIPPED": "补发已出库，尚未签收", "COMPLETED": "办理完成", "REJECTED": "申请未通过", "CANCELLED": "申请已撤销", "NEEDS_REVIEW": "售后需人工核实"}
TYPES = {"REFUND_ONLY": "仅退款", "RETURN_REFUND": "退货退款", "EXCHANGE": "同款同规格换货"}
CASE_STATES['WAITING_CHOICE'] = '换货待选择，请选择继续等待或预览转退款'
CASE_STATES['REPLACEMENT_RELEASING'] = '预占超时，正在核实库存释放，尚不能转退款'
QUESTIONS = {"order": "请在「我的订单」选择你本人要咨询的订单，再进入该订单客服。", "item": "需要办理订单里的哪件商品？请告诉我商品名称或展示编号。", "quantity": "需要办理几件？请明确数量。", "type": "请选择仅退款、退货退款，或同款同规格换货。", "reason": "请说明这次申请或催办的原因。", "question": "你想了解政策、订单进度，还是申请售后？"}
QUESTIONS["tracking"] = "请提供实际寄回运单号；收到单号后才能生成待确认的登记草稿。"
QUESTIONS["order"] = "客服只能办理本人订单及其关联售后，不能把他人的售后单关联到你的工单。请在「我的订单」选择你本人要咨询的订单，再进入该订单客服。"


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def evidence(kind, fields, *, run_id):
    return {"id": "fact:" + str(uuid.uuid4()), "kind": kind, "runId": run_id, "observedAt": datetime.now(timezone.utc).isoformat(),
            "fields": fields, "contentSha256": digest(fields)}


def minor_money(amount, currency):
    if type(amount) is not int or amount < 0 or not isinstance(currency, str):
        raise ValueError("invalid authoritative money")
    return f"{amount // 100}.{amount % 100:02d} {currency}"


async def execute_plan(plan, *, order, retrieval, java, run_id, tool_receipts, question=""):
    """The injected Java adapter holds the user's credential; the model never receives it."""
    order_id = order["id"]
    result = {"kind": plan.intent, "answer": "", "citations": [], "preview": None, "ticketDraft": None, "actionDraft": None}
    async def read(path, method="GET", body=None):
        started = time.perf_counter()
        receipt = {"toolCallId": str(uuid.uuid4()), "runId": run_id, "method": method, "path": path, "requestSha256": digest(body), "status": "STARTED"}
        tool_receipts.append(receipt)
        try:
            response = await java(method, path, **({"body": body} if body is not None else {}))
            receipt.update(status="SUCCEEDED", responseSha256=digest(response))
            return response
        except Exception as exc:
            receipt.update(status="FAILED", errorType=type(exc).__name__)
            if isinstance(exc, HTTPException):receipt['statusCode']=exc.status_code
            raise
        finally:
            receipt["durationMs"] = round((time.perf_counter() - started) * 1000, 3)
    def fact(kind, fields):
        result["citations"].append(evidence(kind, fields, run_id=run_id))
    if plan.intent == "policy":
        result.update(render_selected_policy(retrieval, plan.policy_ids))
        if plan.subject=='current_order' and order.get('_supportCases') and any(
                value.endswith((':replacement-expiry',':exchange-stock')) for value in plan.policy_ids):
            from .planner import SupportPlan
            progress=await execute_plan(SupportPlan(intent='after_sale',subject='current_order'),
                order=order,retrieval=retrieval,java=java,run_id=run_id,tool_receipts=tool_receipts,question=question)
            result['answer']+='\n\n本单实际进度：\n'+progress['answer']
            result['citations'].extend(progress['citations'])
        if plan.subject=='current_order' and any(value.endswith(':tickets') for value in plan.policy_ids) and any(term in question for term in ('退款', '换货', '售后')):
            from .planner import SupportPlan
            progress=await execute_plan(SupportPlan(intent='ticket_status',subject='current_order'),
                order=order,retrieval=retrieval,java=java,run_id=run_id,tool_receipts=tool_receipts)
            result['answer']+='\n\n本单工单与售后进度：\n'+progress['answer']
            result['citations'].extend(progress['citations'])
    elif plan.intent == "clarify":
        result["answer"] = QUESTIONS[plan.missing]
    elif plan.intent == "unsupported":
        result["answer"] = "这项问题暂缺适用证据或不在自动办理范围，可提交当前订单的信息核实工单。"
    elif plan.intent == "product":
        from .product_knowledge import retrieve_product
        item = order['items'][plan.item_number - 1]
        if item.get('itemType') != 'PRODUCT': raise ValueError('unsupported item kind')
        if plan.product_topic == 'original_specification':
            try: original = json.loads(item.get('evidenceJson') or '{}')
            except (ValueError, TypeError): original = {}
            specification = original.get('saleSpecification') if isinstance(original, dict) else None
            if not isinstance(specification, dict) or not specification.get('code'):
                result['answer'] = '原订单没有可靠的规格快照，不能根据当前目录或商品标题补齐。请提交信息核实工单。'
            else:
                text = specification.get('label') or '；'.join(str(specification[key]) for key in ('color', 'storage') if specification.get(key)) or str(specification['code'])
                result['answer'] = '原订单记录的销售规格：' + text + '。换货仍须以服务端资格预览为准。'
                fact('order_specification', {'orderId': order_id, 'itemId': item['itemId'], 'specification': specification})
        elif plan.product_topic == 'original_price':
            result['answer'] = '原订单商品单价为 ' + minor_money(item.get('unitPriceMinor'), order.get('currency')) + '；这不是扣除订单优惠后可退的金额，退款须按原实付分摊预览。'
            fact('order_item_price', {'orderId': order_id, 'itemId': item['itemId'], 'unitPriceMinor': item.get('unitPriceMinor'), 'currency': order.get('currency')})
        else:
            product = await read(f"/api/products/{item['itemId']}")
            if str(product.get('id')) != str(item['itemId']): raise ValueError('product identity mismatch')
            if plan.product_topic == 'catalog_price':
                if product.get('priceStatus') == 'verified' and type(product.get('snapshotPriceMinor')) is int:
                    result['answer'] = '当前目录已验证的快照价格为 ' + minor_money(product['snapshotPriceMinor'], product.get('currency')) + '；这不是原订单实付或退款金额。'
                else:
                    result['answer'] = '当前目录没有可确认的价格，不能据此计算退款金额。请以原订单实付和售后预览为准。'
                fact('catalog_price', {key: product.get(key) for key in ('id', 'snapshotPriceMinor', 'currency', 'priceStatus', 'entityVersion')})
            else:
                found = retrieve_product(question, product, expected_item_id=item['itemId'])
                result['citations'] = found['citations']
                result['answer'] = ('当前目录检索到以下商品资料原文；它不替代原订单规格，也不构成额外赠品或售后承诺：\n' + '\n'.join(f"[{i + 1}] {row['text']}" for i, row in enumerate(found['citations']))) if found['citations'] else '当前商品资料没有检索到支持这个问题的证据，不能凭常识猜测配置、赠品或兼容性。可提交信息核实工单。'
    elif plan.intent == "order":
        status = ORDER_STATES.get(order.get("status"))
        if status is None: raise ValueError("unknown order state")
        amount = minor_money(order.get("payableMinor"), order.get("currency"))
        result["answer"] = f"当前订单：{status}；订单应付金额为 {amount}。支付和物流结果须分别以对应记录核对。"
        fact("order", {key: order.get(key) for key in ("id", "status", "payableMinor", "currency")})
    elif plan.intent == "payment":
        try:
            row = await read(f"/api/payments/orders/{order_id}")
        except HTTPException as exc:
            if exc.status_code != 404 or exc.detail != '订单对应的支付单不存在':raise
            result['answer']='当前订单尚无支付记录；这不表示支付失败，也不能据此认定已经付款或退款到账。'
            fact('payment_absent',{'orderId':order_id,'recordExists':False,'sourceStatusCode':404})
            return result
        if row.get("orderId") != order_id or row.get("status") not in PAYMENT_STATES: raise ValueError("payment identity or state mismatch")
        result["answer"] = f"支付记录：{PAYMENT_STATES[row['status']]}，记录金额 {minor_money(row.get('amountMinor'), row.get('currency'))}。这不代表售后退款已经到账。"
        fact("payment", {key: row.get(key) for key in ("id", "orderId", "status", "amountMinor", "currency", "paidAt", "version")})
    elif plan.intent == "logistics" and plan.logistics_scope == "original_order":
        row = await read(f"/api/orders/{order_id}/fulfillment")
        if row.get("orderId") != order_id or row.get("status") not in LOGISTICS_STATES: raise ValueError("fulfillment identity or state mismatch")
        result["answer"] = "物流状态：" + LOGISTICS_STATES[row["status"]] + "。"
        if row.get("trackingNo"): result["answer"] += "模拟运单：" + str(row["trackingNo"]) + "。"
        result["answer"] += "当前没有承诺送达时间；需要催办可提交物流工单。"
        fact("logistics", {key: row.get(key) for key in ("orderId", "status", "trackingNo", "updatedAt")})
    elif plan.intent == "after_sale" or (plan.intent == "logistics" and plan.logistics_scope == "replacement"):
        rows = await read(f"/api/after-sales/orders/{order_id}")
        if not isinstance(rows, list) or any(row.get("orderId") != order_id or row.get("phase") not in CASE_STATES or row.get("type") not in TYPES for row in rows):
            raise ValueError("aftersale identity or state mismatch")
        replacement = plan.intent == "logistics" and plan.logistics_scope == "replacement"
        if replacement: rows = [row for row in rows if row['type'] == 'EXCHANGE']
        lines = []
        for row in rows:
            state = '补发已签收，换货完成' if row['type'] == 'EXCHANGE' and row['phase'] == 'COMPLETED' else CASE_STATES[row['phase']]
            line = f"{TYPES[row['type']]}：{state}，申请数量 {row['quantity']} 件。"
            if row['phase'] in {'REPLACEMENT_READY', 'REFUND_PENDING'}:
                outcome = '实际出库结果' if row['phase'] == 'REPLACEMENT_READY' else '实际到账结果'
                line += f'如果渠道回执尚未同步，仅凭当前系统状态无法确定{outcome}；可提交核实工单。'
            if '回执' in question:
                receipts = await read(f"/api/after-sales/{row['id']}/receipts")
                if not isinstance(receipts, list) or any(r.get('caseId') != row['id'] or r.get('status') not in {'PENDING', 'APPLIED', 'NEEDS_REVIEW'} for r in receipts):
                    raise ValueError('receipt identity or status mismatch')
                pending = [r for r in receipts if r['status'] != 'APPLIED']
                labels = {'REPLACEMENT_DISPATCH_CONFIRMED': '补发出库', 'REFUND_RECEIPT_CONFIRMED': '退款渠道'}
                for receipt in pending:
                    label = labels.get(receipt.get('eventType'), '售后业务')
                    line += f"已记录{label}回执，" + ('待同步至售后状态。' if receipt['status'] == 'PENDING' else '需要核实后再同步。')
                if not pending:
                    line += '当前未查到待同步的售后回执；这不能证明渠道没有发生相应事件。'
                fact('receipt_status', {'caseId': row['id'], 'orderId': order_id, 'receipts': receipts})
            if row['type']=='EXCHANGE' and row['phase'] in {'WAITING_STOCK','REPLACEMENT_RELEASING'}:
                line+='库存结果尚待核实，当前阶段不允许直接转退款；结果未知不等于已确认缺货。'
            if row['type'] != 'EXCHANGE':
                amount = minor_money(row.get('amountMinor'), row.get('currency'))
                line += ('已退款 ' if row['phase'] == 'COMPLETED' else '申请金额 ') + amount + '。'
                if row['phase'] != 'COMPLETED': line += '该金额不代表已经到账。'
            if replacement and row['phase'] in {'REPLACEMENT_SHIPPED', 'COMPLETED'}:
                events = await read(f"/api/after-sales/{row['id']}/events")
                expected_event = 'REPLACEMENT_RECEIPT_CONFIRMED' if row['phase'] == 'COMPLETED' else 'REPLACEMENT_DISPATCH_CONFIRMED'
                matching = [event for event in events if event.get('type') == expected_event]
                if matching:
                    event = matching[-1]
                    details = json.loads(event['evidence'])
                    payload = details.get('payload', {})
                    import re
                    tracking = payload.get('trackingNo')
                    if str(payload.get('itemId')) != str(row['itemId']) or payload.get('quantity') != row['quantity'] or not isinstance(tracking, str) or not re.fullmatch(r'[A-Za-z0-9_-]{3,128}', tracking):
                        raise ValueError('replacement logistics evidence mismatch')
                    line += '补发模拟运单：' + tracking + '。'
                    fact('replacement_logistics', {'caseId': row['id'], 'orderId': order_id, 'eventId': event['id'], 'eventType': expected_event, 'trackingNo': tracking})
                else:
                    line += '未查到可核对的补发运单记录，可提交核实工单。'
            lines.append(line)
        result['answer'] = '\n'.join(lines) if lines else ('当前订单没有查到换货补发申请。' if replacement else '当前订单没有查到售后申请。')
        fact("after_sale", {"orderId": order_id, "cases": [{key: row.get(key) for key in ("id", "type", "phase", "itemId", "quantity", "amountMinor", "currency", "version")} for row in rows]})
    elif plan.intent == "preview":
        item = order["items"][plan.item_number - 1]
        if item.get("itemType") != "PRODUCT": raise ValueError("unsupported item kind")
        body = {"orderId": order_id, "itemId": item["itemId"], "quantity": plan.quantity, "type": plan.after_sale_type, "reason": plan.reason_quote}
        row = await read("/api/after-sales/preview", "POST", body)
        if row.get("orderId") != order_id or str(row.get("itemId")) != str(item["itemId"]) or row.get("quantity") != plan.quantity or row.get("type") != plan.after_sale_type:
            raise ValueError("preview identity mismatch")
        minor_money(row.get("amountMinor"), row.get("currency"))
        result.update(answer=f"已生成{TYPES[plan.after_sale_type]}预览，请核对商品、数量和金额，再点击确认。申请尚未提交。",
                      preview={**row, "title": item["titleSnapshot"], "label": TYPES[plan.after_sale_type], "conversion": False})
        fact("preview", {key: row.get(key) for key in ("previewId", "orderId", "itemId", "quantity", "type", "amountMinor", "currency", "expiresAt", "policyVersion")})
    elif plan.intent in {'ticket_status','ticket_reply'}:
        tickets=[];offset=0
        # Bound work and never select from an incomplete list.
        while offset<500:
            page=await read(f'/api/support/tickets?offset={offset}&limit=100')
            if not isinstance(page,list):raise ValueError('invalid ticket list')
            tickets.extend(row for row in page if row.get('orderId')==order_id)
            if len(page)<100:break
            offset+=100
        if offset>=500:
            result['answer']='工单较多，请在下方工单列表选择具体记录后查看或补充。'
        elif plan.intent=='ticket_reply':
            if len(tickets)!=1:
                result['answer']='请在下方工单列表选择要补充的具体工单，再填写回复。'
            else:
                ticket=tickets[0]
                if ticket['status']=='CLOSED':
                    result['answer']='这笔工单已关闭，不能追加回复；如仍有争议，请新建关联工单。'
                    policy = next((row for row in retrieval.get('citations', []) if row.get('id', '').endswith(':tickets')), None)
                    if policy is not None:
                        result['citations'].append(policy)
                elif not plan.reply_quote.strip():result['answer']='请填写要补充给这笔工单的回复内容，核对后再确认提交。'
                else:
                    result.update(answer='已整理补充说明，请核对后点击确认回复。尚未写入工单。',
                                  ticketReplyDraft={'orderId':order_id,'ticketId':ticket['id'],'body':{'expectedVersion':ticket['version'],'message':plan.reply_quote}})
        else:
            states={'OPEN':'待处理','WAITING_CUSTOMER':'等待你补充资料','RESOLVED':'已处理','CLOSED':'已关闭'}
            if any(t.get('status') not in states for t in tickets):raise ValueError('unknown ticket status')
            result['answer']='\n'.join(f"工单 {t['id']}：{states[t['status']]}。" for t in tickets) or '当前订单没有查到工单。'
            result['answer']+='工单处理或关闭不代表退款到账，也不代表售后已撤销。'
            from .planner import SupportPlan
            progress=await execute_plan(SupportPlan(intent='after_sale',subject='current_order'),order=order,retrieval=retrieval,java=java,run_id=run_id,tool_receipts=tool_receipts)
            result['answer']+='\n'+progress['answer'];result['citations'].extend(progress['citations'])
        fact('tickets',{'orderId':order_id,'tickets':[{k:t.get(k) for k in ('id','orderId','caseId','category','status','version')} for t in tickets]})
    elif plan.intent == "ticket_draft":
        cases=order.get('_supportCases',[])
        target=cases[plan.case_number-1] if plan.case_number else None
        if target is None and plan.ticket_category=='AFTERSALE_DISPUTE' and len(cases)==1:
            target=cases[0]
        result.update(answer="已整理工单草稿，请核对问题说明后点击提交。尚未登记工单。",
                      ticketDraft={"orderId": order_id, "caseId": target['id'] if target else None, "category": plan.ticket_category, "summary": plan.reason_quote})
        if target and target.get('type')=='EXCHANGE' and target.get('phase')=='COMPLETED' and plan.ticket_category=='AFTERSALE_DISPUTE':
            fact('dispute_case',{key:target.get(key) for key in ('id','orderId','type','phase','itemId','quantity','version')})
            selected=[c['id'] for c in retrieval.get('citations',[]) if c['id'].endswith(':replacement-dispute')]
            if selected:
                policy=render_selected_policy(retrieval,selected)
                result['answer']=policy['answer']+'\n'+result['answer']
                result['citations'] = policy['citations'] + result['citations']
    elif plan.intent == 'case_action':
        target = order['_supportCases'][plan.case_number - 1]
        row = await read(f"/api/after-sales/{target['id']}")
        if row.get('id') != target['id'] or row.get('orderId') != order_id: raise ValueError('case action ownership mismatch')
        allowed = {'cancel': {'AWAITING_REVIEW', 'AWAITING_RETURN'}, 'return_shipment': {'AWAITING_RETURN'}, 'wait_stock': {'WAITING_CHOICE'}, 'conversion_preview': {'WAITING_CHOICE'}}
        if row.get('phase') not in allowed[plan.case_action]:
            result['answer'] = '当前售后阶段不允许这项操作，请刷新进度；需要争议处理可提交关联工单。'
            fact('after_sale', {key: row.get(key) for key in ('id', 'orderId', 'phase', 'version')})
        elif plan.case_action == 'conversion_preview':
            preview = await read(f"/api/after-sales/{row['id']}/conversion-preview", 'POST')
            if preview.get('caseId') != row['id']: raise ValueError('conversion preview identity mismatch')
            minor_money(preview.get('amountMinor'), preview.get('currency'))
            item = next((item for item in order['items'] if str(item['itemId']) == str(row['itemId'])), None)
            if item is None: raise ValueError('case item absent from owned order')
            result.update(answer='已生成转退货退款预览，请核对金额后点击确认。换货申请尚未变更。',
                          preview={**preview, 'orderId': order_id, 'title': item['titleSnapshot'], 'quantity': row['quantity'], 'specification': row.get('specification'), 'label': '转为退货退款', 'conversion': True})
        else:
            labels = {'cancel': '撤销售后申请', 'return_shipment': '登记寄回单号', 'wait_stock': '继续等待换货库存'}
            body = {'expectedVersion': row['version']}
            if plan.case_action == 'return_shipment': body['trackingNo'] = plan.tracking_quote
            result.update(answer='请核对下面的售后操作，点击确认后才会提交。', actionDraft={'orderId': order_id, 'caseId': row['id'], 'action': plan.case_action, 'label': labels[plan.case_action], 'body': body})
            fact('after_sale', {key: row.get(key) for key in ('id', 'orderId', 'phase', 'version')})
    else:
        raise ValueError("unsupported plan intent")
    if plan.intent in {'payment', 'after_sale'} and '退款' in question and any(term in question for term in ('支付', '付款')):
        from .planner import SupportPlan
        other = 'after_sale' if plan.intent == 'payment' else 'payment'
        combined = await execute_plan(SupportPlan(intent=other, subject='current_order'), order=order,
            retrieval=retrieval, java=java, run_id=run_id, tool_receipts=tool_receipts)
        result['answer'] += '\n' + combined['answer']
        result['citations'].extend(combined['citations'])
    return result
