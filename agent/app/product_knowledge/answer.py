"""Parent-model selection with deterministic identity/citation publication checks."""
import json
from copy import deepcopy

from .comparison import CONTRACT

INSTRUCTION = '''你是购物父Agent，依据提供的原始需求、当前硬条件和型号证据做需求权衡。
商品标题不是性能实测。只使用输入事实，不联网、不凭常识补参数。否定需求不能改写为正向需求。
输出JSON：{"recommendations":[{"productId":"精确字符串ID","reason":"有依据的取舍推断","evidenceIds":["输入的证据ID"]}],"unknowns":["缺口"]}。
最多3件，每件reason不超过80字，unknowns最多3条；不能推荐硬条件失败的商品；硬条件未知只能作为待核验备选。不同测试条件不可直接判胜负。
reason只解释引用证据支持的取舍，不重写预算、价格或机况；这些由程序核验。不要引入型号定位或常识，不发明参数/跑分/游戏帧率，不许宣称这台二手机一定具有标准型号的表现。
每条引用必须属于该商品可绑定型号。事实文本会由程序从证据库原样展示，不需要你重写。
电池容量/能量不能推出续航长短或续航潜力排名；相机像素/镜头数量不能推出拍照优劣；芯片名称不能推出游戏帧率或稳定性。
只能在资料可比且覆盖待比较商品时讨论实测差异。不能把检索返回的一小部分证据当作全部候选的完整参数，禁止称最高、最大、最优、相对突出。
没有可比实测时，可根据已核验参数解释为何值得进一步核验，但必须明说性能/场景适合度尚不能判定。
无法支持性能优劣时明确承认没有足够证据，可以不推荐。'''
INSTRUCTION += '\nunknowns请输出空数组，资料缺口由程序根据知识库记录显示。reason只描述本轮取得的证据，不能称仅此/唯一型号有证据，不能断言其他或所有候选没有资料。'


def model_input(payload):
    """Keep seller advertising out of model deliberation; retain it for UI."""
    value=deepcopy(payload)
    displayed={str(i) for i in value.get('sourceDisplayIds',[])}
    if displayed:
        value['products']=[p for p in value['products'] if str(p['id']) in displayed]
        value['candidates']=[c for c in value.get('candidates',[]) if str(c['productId']) in displayed]
    bindings={b['itemId']:b for b in value['knowledge'].get('bindings',[])}
    for product in value['products']:
        binding=bindings.get(str(product['id']),{})
        product['title']=(' / '.join(binding.get('modelKeys',[]))
            if binding.get('status')=='CLEAR' else '型号身份尚未确认')
        product['id']=str(product['id'])
        amount=product.get('syntheticReferencePriceMinor')
        if type(amount) is int:product['syntheticReferencePriceYuan']=f'{amount/100:.2f}（AI合成，非真实报价）'
    for requirement in value.get('requirements',[]):
        if requirement.get('key')=='price_minor' and type(requirement.get('value')) is int:
            requirement['displayExpectedYuan']=f"{requirement['value']/100:.2f}"
    return value


def payload_from_view(view):
    for result in getattr(view,'validated_results',[]) or []:
        evidence=result.get('evidence',{})
        if evidence.get('contractVersion')==CONTRACT:
            return evidence
    return None


def budget_lines(payload):
    """Disclose the first-pass interpretation of approximate budgets."""
    if not any(cue in payload.get('userQuery', '') for cue in ('左右', '上下')):
        return []
    ceiling=next((r.get('value') for r in payload.get('requirements', [])
                  if r.get('key')=='price_minor' and r.get('operator')=='lte'
                  and r.get('priority')=='hard'), None)
    if type(ceiling) not in (int, float):
        return []
    return [f'本轮先按不超过 ¥{ceiling/100:,.2f}、优先接近预算筛选；如可适当超出预算，可继续调整。']


def fallback(payload):
    knowledge=payload['knowledge']
    lines=['型号知识暂不可用。以下仅保留商品事实核验。' if knowledge.get('status')=='UNAVAILABLE'
           else '现有证据不足以可靠给出性能优劣结论，先保留商品事实与缺口。']
    missing_target=bool(knowledge.get('requestedModelKeys')) and not knowledge.get('modelKeys')
    bindings={str(b['itemId']):b for b in knowledge.get('bindings',[]) if b.get('status')=='CLEAR'}
    candidates=payload['candidates']
    displayed=[str(i) for i in payload.get('sourceDisplayIds',[])]
    if displayed:
        by_id={str(c['productId']):c for c in candidates}
        candidates=[by_id[i] for i in displayed if i in by_id]
    for candidate in ([] if missing_target else candidates[:3]):
        rows=candidate['decision']['products']
        status=('符合已核验条件' if payload.get('requirements') else '尚未登记具体筛选条件') if rows and rows[0]['fullyMatched'] else '硬条件失败或待核验，不能视作满足'
        product=next((p for p in payload['products'] if p['id']==candidate['productId']),{})
        lines.append(f"### {product.get('title','商品')}\n\n{status}。")
        binding=bindings.get(str(candidate['productId']),{})
        matched=[e for e in knowledge.get('evidence',[]) if e['modelKey'] in binding.get('modelKeys',[])
                 and (binding.get('region','UNKNOWN')=='UNKNOWN' or e['source'].get('region','UNKNOWN') in
                      {'UNKNOWN','TEST_SAMPLE',binding['region']})]
        if not any(e.get('field')=='battery_test' for e in matched) and '续航' in payload.get('userQuery',''):
            lines.append('本轮未找到该型号的续航实测；以下容量参数不代表实际续航时长。')
        for evidence in matched[:5]:
            source=evidence['source']
            lines.append(f"- 型号参考：{evidence['text']} [资料来源]({source['url']})；适用版本：{source.get('region','UNKNOWN')}。")
            if source.get('testConditions') or source.get('softwareVersion'):
                lines.append('测试条件：'+json.dumps({'software':source.get('softwareVersion'),'conditions':source.get('testConditions')},ensure_ascii=False))
    lines.append('型号资料不能证明实物机况；价格和库存以交易端核验为准。')
    lines.extend(gap_lines(knowledge))
    return '\n\n'.join(budget_lines(payload)+lines)


def gap_lines(knowledge):
    labels={'chip':'芯片','camera':'相机规格','screen':'屏幕','battery':'电池规格','charging':'充电',
            'gaming_test':'游戏实测','camera_test':'拍照实测','battery_test':'续航实测'}
    fields=list(dict.fromkeys(f for gap in knowledge.get('gaps',[]) for f in
        (gap.get('fields',[]) or ([gap['field']] if 'field' in gap else []))))
    lines=[]
    if fields:lines.append('资料缺口（部分候选）：'+ '、'.join(labels.get(f,f) for f in fields)+'；未收录不等于不具备。')
    if any(g.get('reason')=='REQUESTED_MODEL_NOT_IN_CANDIDATES' for g in knowledge.get('gaps',[])):
        lines.append('当前候选没有可明确绑定的指定型号，请重新检索该型号；不能用其他型号代答。')
    return lines


def render(raw, payload, *, on_validation=None):
    try:
        if raw.strip().startswith('```'):
            import re
            fenced=re.fullmatch(r'```(?:json)?\s*([\s\S]*?)\s*```',raw.strip())
            if fenced:raw=fenced.group(1)
        draft=json.loads(raw)
        recommendations=draft['recommendations']; unknowns=draft['unknowns']
        if not isinstance(recommendations,list) or len(recommendations)>3 or not isinstance(unknowns,list): raise ValueError('shape')
        products={str(p['id']):p for p in payload['products']}
        checked={str(c['productId']):c['decision'] for c in payload['candidates']}
        knowledge=payload['knowledge']; evidence={e['evidenceId']:e for e in knowledge.get('evidence',[])}
        bindings={b['itemId']:b for b in knowledge.get('bindings',[]) if b['status']=='CLEAR'}
        seen=set(); lines=[]
        for item in recommendations:
            iid=item['productId']; reason=item['reason']; refs=item['evidenceIds']
            if type(iid) is not str or iid not in products or iid in seen: raise ValueError('identity')
            displayed={str(i) for i in payload.get('sourceDisplayIds',[])}
            if displayed and iid not in displayed:raise ValueError('outside_displayed_products')
            if not isinstance(reason,str) or not 1<=len(reason)<=1000 or not isinstance(refs,list) or len(refs)>8: raise ValueError('reason')
            rows=checked[iid]['products']
            if not rows or rows[0].get('hardFailures'): raise ValueError('hard_failure_recommended')
            model_keys=bindings.get(iid,{}).get('modelKeys',[])
            requested=knowledge.get('requestedModelKeys',[])
            if requested and not set(model_keys)&set(requested):raise ValueError('requested_model_mismatch')
            if any(r not in evidence or evidence[r]['modelKey'] not in model_keys for r in refs): raise ValueError('citation')
            region=bindings.get(iid,{}).get('region','UNKNOWN')
            if any(region!='UNKNOWN' and evidence[r]['source'].get('region','UNKNOWN') not in
                   {'UNKNOWN','TEST_SAMPLE',region} for r in refs):raise ValueError('listing_region_mismatch')
            if not refs:
                reason='暂无可引用的型号性能证据；这里只核验商品事实，不能判断性能优劣。'
            else:
                import re
                if re.search(r'\d\s*成新|\d\s*新|(?:预算|价格|上限).{0,6}\d|\d+.{0,3}上限',reason):
                    raise ValueError('price_or_condition_must_use_checked_renderer')
                if re.search(r'按常理|凭常识|按常识|定位较高|定位更高',reason):raise ValueError('unsupported_prior_knowledge')
                if re.search(r'仅此|唯一|其余.{0,12}(?:无|没有)|(?:所有|全部|均).{0,12}(?:无|没有)',reason):raise ValueError('unsupported_coverage_claim')
                if re.search(r'最高|最大|最优|相对突出|续航潜力|(?:高于|低于|优于|超过).{0,12}(?:其他|其它|所有)|(?:其他|其它|所有).{0,12}(?:高于|低于|优于|超过)',reason):raise ValueError('unsupported_global_or_capacity_ranking')
                if (re.search(r'更强|更好|最强|完胜|吊打|流畅|稳.{0,3}(?:帧|fps)',reason)
                        and not re.search(r'无法|不能|不代表|未验证|缺少|没有',reason)):
                    raise ValueError('unverified_performance_superiority')
            seen.add(iid)
            state=('条件内建议' if refs else '事实备选，性能待核验') if rows[0]['fullyMatched'] else '待核验备选（存在硬条件未知）'
            original=[str(i) for i in payload.get('sourceDisplayIds',[])]
            position=f'原卡片第{original.index(iid)+1}项，' if iid in original else '候选范围内其他商品，'
            lines.append(f"{state}：{products[iid]['title']}（{position}ID {iid}）\n取舍推断：{reason}")
            price=products[iid].get('syntheticReferencePriceMinor')
            if type(price) is int:lines.append(f'模拟参考价：¥{price/100:.2f}（AI 合成，非真实报价）。')
            for ref in dict.fromkeys(refs):
                e=evidence[ref]; s=e['source']
                lines.append(f"- 型号参考：{e['text']} [{ref}]({s['url']})；适用版本：{s.get('region','UNKNOWN')}。")
                if s.get('testConditions') or s.get('softwareVersion'):
                    lines.append('  测试条件：'+json.dumps({'software':s.get('softwareVersion'),'conditions':s.get('testConditions')},ensure_ascii=False))
        if not lines:
            if on_validation:on_validation('MODEL_ABSTAINED')
            return fallback(payload)
        if any(not isinstance(x,str) or len(x)>500 for x in unknowns): raise ValueError('unknowns')
        if knowledge.get('status')=='UNAVAILABLE':lines.insert(0,'型号知识暂不可用；以下只核验商品事实，未联网补答。')
        # Lack of a retrieved snippet does not establish absence in the store.
        # Publish only service-owned gaps, not model-authored coverage counts.
        lines.extend(gap_lines(knowledge))
        lines.append('商品卡片保留原顺序；以上为注明版本的型号参考，未核验卖家实物，不保证当前二手机性能、续航、价格或库存。')
        if on_validation:on_validation('VALIDATED')
        return '\n'.join(budget_lines(payload)+lines)
    except (ValueError,KeyError,TypeError) as exc:
        if on_validation:on_validation(type(exc).__name__+':'+str(exc)[:120])
        return fallback(payload)
