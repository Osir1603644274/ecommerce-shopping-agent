"""Source-specific product facts, checked without importing retrieval/runtime code."""
import json
import re
from business_oracle import _result

def grade_product(scenario,observed,before,after):
    trace=observed.get('trace') or {};result=trace.get('result') or {};answer=result.get('answer','');citations=result.get('citations',[])
    topic=scenario['expected']['routeOrOutcome'].split(':')[1];item=before['order_item'][0];product=before['product'][0];order=before['customer_order'][0]
    checks=[('completed',trace.get('status')=='COMPLETED'),('read_only',before==after),('no_write_draft',not any(result.get(k) for k in ('preview','ticketDraft','actionDraft','ticketReplyDraft')))]
    def fact(kind,fields):
        found=[c.get('fields',{}) for c in citations if c.get('kind')==kind]
        return len(found)==1 and all(found[0].get(k)==v for k,v in fields.items())
    if topic=='missing_attributes':
        # This fixture asks specifically for water-resistance evidence; do not
        # generalize absence to every question or import the SUT retriever.
        source=product.get('attribute_text') or ''
        tools=[t for attempt in trace.get('attempts',[]) for t in attempt.get('toolReceipts',[])]
        refusal='当前商品资料没有检索到支持这个问题的证据，不能凭常识猜测配置、赠品或兼容性。可提交信息核实工单。'
        checks += [('product_identity',product['id']==item['item_id']),
                   ('water_resistance_missing',not re.search(r'防水|waterproof|water.resistan|\bIP\s*\d{2}\b',source,re.I)),
                   ('product_actually_read',any(t.get('method')=='GET' and t.get('path')=='/api/products/'+str(product['id']) and t.get('status')=='SUCCEEDED' for t in tools)),
                   ('missing_evidence_disclosed',answer==refusal),('no_invented_citations',not citations)]
    elif topic=='original_specification':
        spec=json.loads(item.get('evidence_json') or '{}').get('saleSpecification')
        if not spec:checks += [('missing_spec_disclosed','没有可靠的规格快照' in answer),('no_fabricated_spec',not citations)]
        else:
            checks += [('original_spec_source',fact('order_specification',{'orderId':order['id'],'itemId':item['item_id'],'specification':spec})),('original_spec_answer',spec.get('label',spec['code']) in answer)]
    elif topic in {'original_price','catalog_price'}:
        original=topic=='original_price';row=item if original else product;key='unit_price_minor' if original else 'snapshot_price_minor'
        if not original and row['price_status']!='verified':checks.append(('unverified_price','没有可确认的价格' in answer))
        else:
            amount=row[key];money=f"{amount//100}.{amount%100:02d} {order['currency']}"
            checks.append(('price_answer',money in answer))
        fields={'orderId':order['id'],'itemId':item['item_id'],'unitPriceMinor':row[key],'currency':order['currency']} if original else {'id':product['id'],'snapshotPriceMinor':row[key],'currency':row['currency'],'priceStatus':row['price_status'],'entityVersion':row['entity_version']}
        checks.append(('price_source',fact('order_item_price' if original else 'catalog_price',fields)))
    else:
        fragments=[s.strip() for s in re.split(r'[\n；;。]+',product.get('attribute_text') or '') if s.strip()]
        checks.append(('source_quotes',bool(citations) and all(c.get('kind')=='product' and str(c.get('itemId'))==str(product['id']) and c.get('text') in fragments and c.get('text') in answer for c in citations)))
        message=scenario['steps'][0]['message'];keyword='充电接口' if '接口' in message else '存储容量' if '容量' in message else None
        if keyword:checks.append(('question_relevance',any(keyword in c.get('text','') for c in citations)))
        checks.append(('catalog_boundary','不替代原订单规格' in answer))
    return _result(checks,[] if before==after else ['unconfirmed_business_write'])
