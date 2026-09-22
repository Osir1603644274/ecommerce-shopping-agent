"""Owner-bound public-search workflow on the existing workspace controls.

Checkpoints are ordinary workspace run records, not forged GraphV2 receipts.
Each completed phase is durable before the next phase starts.
"""
from copy import deepcopy
from contextlib import asynccontextmanager
import asyncio
import time
import re

from fastapi import HTTPException

from . import commerce_workspace as ws
from ..catalog_conversation import transition, answer_turn, apply_subject_review
from ..catalog_service import get_catalog_service, fingerprint, workflow_code_binding, verify_scope

PHASES = ['prepare', 'retrieve', 'answer', 'publish']
LABELS = {'prepare': '整理当前需求', 'retrieve': '检索两个商品来源', 'answer': '依据证据回答', 'publish': '保存本轮结果'}


@asynccontextmanager
async def worker_lock(key):
    # The API launches its background job before releasing its owner lock.
    # A worker waits for that short critical section; browser commands retain
    # the original immediate-409 policy instead of silently queueing writes.
    deadline = time.monotonic()+10
    while True:
        context = ws._lock(key)
        try:
            await context.__aenter__()
            break
        except HTTPException as exc:
            if exc.status_code!=409 or time.monotonic()>=deadline:
                raise
            await asyncio.sleep(.025)
    try:
        yield
    finally:
        await context.__aexit__(None,None,None)


async def checkpoint(key, run, *, phase=None, seconds=0):
    from .commerce_controls import save_run
    async with worker_lock(key):
        saved = await ws._load(key+':run')
        if not saved or saved['id']!=run['id'] or saved['status']=='ended':
            raise HTTPException(409, '当前运行已变更')
        run['pauseRequested'] = saved.get('pauseRequested', False)
        labels = ({**LABELS, 'retrieve':'读取所指商品记录', 'answer':'回答商品问题'}
                  if run['catalogPlan']['route']=='product' else LABELS)
        if phase:
            from ..catalog_execution_view import phase_fields
            public_fields = phase_fields(run,phase,seconds) if run['catalogPlan']['route']=='catalog' else {}
            run['nodes'].append({'id': run['id']+':catalog:'+phase+(f":{len(run['nodes'])}" if run.get('catalogReact') else ''), 'label': labels[phase],
                'outcome': 'completed', 'durationMs': seconds*1000,
                'code': 'agent/app/api/catalog_workspace.py:work',
                'detail': {'workflow': 'product_question_v1' if run['catalogPlan']['route']=='product' else 'catalog_workspace_v1', 'phase': phase,
                    'scopeId': (run.get('catalogNext', {}).get('scope') or {}).get('scopeId')}, **public_fields})
            run['catalogPhase'] += 1
            if run.get('catalogReact') and phase in {'retrieve', 'answer'}:
                run.pop('catalogPendingDecision', None)
                run['catalogDecisionRevision'] = run.get('catalogDecisionRevision', 1) + 1
            if phase=='publish':
                state = await ws._load(key)
                for message in (state or {}).get('messages',[]):
                    if message.get('requestId')==run['requestId'] and message['role']=='assistant':
                        message['flow'] = deepcopy(run['nodes'])
                if state:
                    await ws._save(key,state)
        index = run.get('catalogPhase', 0)
        run['nextStage'] = ('根据执行结果决定下一步' if run.get('catalogReact') and index in {1,2}
            else labels[PHASES[index]] if index<len(PHASES) else 'done')
        if index==len(PHASES):
            run.update(status='completed', notice='本轮商品问答已完成' if run['catalogPlan']['route']=='product' else '本轮商品搜索已完成')
        elif run.get('pauseRequested'):
            run.update(status='paused', notice='已保存商品搜索检查点，可以继续或结束。')
        elif run['mode']=='step':
            run.update(status='waiting', notice='当前步骤已保存，可以执行下一步。')
        run['revision'] = saved['revision']+1
        await save_run(key, run)


async def work(key, run, operation, answer):
    from .commerce_controls import save_run
    try:
        if run.get('catalogCodeBinding') != workflow_code_binding():
            raise ValueError('catalog_workflow_version_changed')
        if operation == 'continue' and run.get('catalogClarification'):
            from ..catalog_conversation import plan_turn
            from ..product_followup import bind_plan
            from ..task_state import get_session_task_state
            state = await ws._load(key)
            plan, receipt = await plan_turn(answer, state)
            plan = await bind_plan(plan, state, await get_session_task_state(state['engine']), answer)
            original_message = run['message']
            run.update(message=answer, requestId=run['replyRequestId'], catalogPlan=plan,
                catalogRouteCall=receipt, catalogPhase=0, catalogBaseRevision=(state.get('catalogSearch') or {}).get('revision',0),
                catalogReact=ws.auth.settings.agent_control_runtime=='react_v1' and ws.auth.settings.agent_react_live_enabled, catalogModelDecisions=0, catalogQueries=[])
            for field in ('catalogClarification','clarification','catalogPendingDecision','catalogNext','catalogBefore','catalogNotice','presentationData','productFacts','catalogEvidenceSha256'):
                run.pop(field,None)
            if plan['route']=='business':
                # Start a server-bound business task; never fabricate a durable resume.
                run.pop('workflow',None)
                run['message']=original_message+'\n用户补充：'+answer
                async with worker_lock(key):
                    state.update(cards=[],selection=None,reference=None)
                    state.pop('catalogSearch',None)
                    state.pop('catalogPendingRequest',None)
                    await ws._save(key,state)
                    await save_run(key,run)
                from .commerce_controls import work as business_work
                return await business_work(key,run,'start',None)
            async with worker_lock(key):
                await save_run(key,run)
        if run['catalogPlan']['route']=='product':
            return await product_work(key,run)
        while run.get('catalogPhase', 0)<len(PHASES):
            if run.get('catalogReact') and run['catalogPhase'] in {1,2}:
                if not await select_catalog_action(key,run):
                    return
            phase = PHASES[run.get('catalogPhase', 0)]
            began = time.perf_counter()
            plan = run['catalogPlan']
            if phase=='prepare':
                async with worker_lock(key):
                    state = await ws._load(key)
                    if state['engine']!=run['engine']:
                        raise HTTPException(409, '对话已切换')
                    current = state.get('catalogSearch') or {}
                    if 'catalogBefore' not in run:
                        run['catalogBefore'] = {k:deepcopy(current.get(k)) for k in ['query','requirements']}
                    replay_prepare = state.get('catalogPendingRequest')==run['requestId']
                    if not replay_prepare and current.get('revision', 0)!=run['catalogBaseRevision']:
                        raise HTTPException(409, '需求版本已变化，请重新提交')
                    next_state, notice = ((deepcopy(current), state.get('catalogPendingNotice'))
                        if replay_prepare else transition(current, plan))
                    run['catalogNext'] = next_state
                    if notice:
                        run['catalogNotice'] = notice
                    # Invalidate references immediately for a changed query.
                    # The pending state is identified by this request on retry.
                    state.update(catalogSearch=next_state, cards=[], selection=None, reference=None)
                    state.pop('productFollowup', None)
                    state['catalogPendingRequest'] = run['requestId']
                    state['catalogPendingNotice'] = notice
                    await ws._save(key, state)
            elif phase=='retrieve':
                if plan['action'] in {'search', 'refine', 'new'}:
                    current=run['catalogNext']
                    retrieval_query=(run.get('catalogPendingDecision') or {}).get('query') or current.get('retrievalQuery') or current['query']
                    kwargs=({'requirements':current['requirements'],'retrieval_query':retrieval_query}
                            if current.get('requirements') else {})
                    run['catalogNext']['scope'] = await get_catalog_service().search(current['query'],**kwargs)
                    if run.get('catalogReact'):
                        run.setdefault('catalogQueries',[]).append(retrieval_query)
                scope = run['catalogNext'].get('scope')
                verify_scope(scope)
                run['catalogEvidenceSha256'] = fingerprint(scope)
            elif phase=='answer':
                if run.get('catalogNotice'):
                    run['catalogAnswer'] = run['catalogNotice']
                else:
                    text, receipt = await answer_turn(run['message'], plan, run['catalogNext'])
                    review = (receipt or {}).get('scopeReview')
                    if review:
                        scope = run['catalogNext']['scope']
                        if review['baseScopeId']!=scope['scopeId'] or fingerprint(scope)!=run['catalogEvidenceSha256']:
                            raise ValueError('catalog_review_evidence_changed')
                        run['catalogRetrievedEvidenceSha256'] = run['catalogEvidenceSha256']
                        run['catalogNext']['scope'] = apply_subject_review(scope, review['reviews'])
                        run['catalogEvidenceSha256'] = fingerprint(run['catalogNext']['scope'])
                    run['catalogAnswer'] = text
                    run['catalogAnswerCall'] = receipt
            else:
                from ..catalog_commerce import resolve_optional_cards, source_only_answer
                from .workspace_answer_stream import compose
                from ..catalog_conversation import compact_evidence
                scope = run['catalogNext'].get('scope')
                # Perform read-only authority resolution before entering the publication lock.
                # Retry may refresh prices, but never grants order-creation authority itself.
                trading_cards, authority_notice = await resolve_optional_cards(scope)
                if authority_notice:
                    visible_answer = source_only_answer(scope, authority_notice)
                else:
                    visible_answer = await compose(key, run, {'answer': run['catalogAnswer'],
                        'candidates': compact_evidence(scope) if scope else []})
                run['catalogTradingSummary']={'resolved':len(trading_cards),'previewEligible':sum(bool(c.get('purchasable')) for c in trading_cards)}
                if authority_notice:
                    run['catalogTradingSummary']['availability'] = 'unavailable_read_only'
                async with worker_lock(key):
                    state = await ws._load(key)
                    if state['engine']!=run['engine'] or state.get('catalogPendingRequest')!=run['requestId']:
                        raise HTTPException(409, '候选已失效，不能发布旧结果')
                    if fingerprint(run['catalogNext'].get('scope'))!=run['catalogEvidenceSha256']:
                        raise ValueError('catalog_evidence_changed')
                    if not any(m['role']=='assistant' and m['requestId']==run['requestId'] for m in state['messages']):
                        state['messages'].append({'role': 'assistant', 'requestId': run['requestId'],
                            'content': visible_answer, 'cards': trading_cards, 'flow': deepcopy(run['nodes']),
                            'source': run.get('source', 'web_unreviewed')})
                    state.update(catalogSearch=run['catalogNext'], cards=trading_cards, selection=None, reference=None)
                    state['messages'] = state['messages'][-60:]
                    # Keep pendingRequest as the idempotent publication key;
                    # a new request replaces it during its prepare phase.
                    await ws._save(key, state)
            await checkpoint(key, run, phase=phase, seconds=time.perf_counter()-began)
            if run['status'] in {'paused', 'waiting', 'completed'}:
                return
    except Exception as exc:
        from .workspace_answer_stream import AnswerPaused
        ws.logger.warning('Catalog workspace phase %s failed: %s', run.get('catalogPhase', 0), type(exc).__name__)
        async with worker_lock(key):
            saved = await ws._load(key+':run')
            if not saved or saved['id']!=run['id'] or saved['status']=='ended':
                return
            run.update(status='paused' if isinstance(exc, AnswerPaused) else 'interrupted', revision=saved['revision']+1,
                notice='本轮商品搜索尚未完成。可以从已保存步骤继续，或结束本轮。',
                catalogError={'type': type(exc).__name__, 'phase': run.get('catalogPhase', 0),
                    'code': str(exc) if re.fullmatch(r'[a-z0-9_]+', str(exc)) else None,
                    'modelCall': getattr(exc, 'receipt', None),
                    'validation': [{k: row[k] for k in ('type', 'loc', 'msg') if k in row}
                        for row in exc.errors(include_input=False, include_url=False)] if hasattr(exc, 'errors') else None})
            if isinstance(exc, AnswerPaused):
                run['notice'] = '已停止输出并保存文字。继续会依据原资料接着生成，不重做搜索或交易。'
            await save_run(key, run)


async def select_catalog_action(key,run):
    """Observe -> decide -> checkpoint selection before any read-only tool.

Replaying a saved selection never asks the model again. It is validated against
the original decision view; no stale scope or unlisted action may be executed.
"""
    from ..catalog_react import decide, decision_view
    from ..control.react_actions import NextAction
    from ..control.react_decision import validate_next_action
    from .commerce_controls import save_run
    selected = run.get('catalogPendingDecision')
    if selected is None:
        selected = await decide(run)
    validation_run = deepcopy(run)
    if run.get('catalogPendingDecision') and selected['source']=='model':
        validation_run['catalogModelDecisions'] -= 1
    view,_ = decision_view(validation_run)
    if selected['viewHash'] != view.decision_view_hash:
        raise ValueError('catalog_decision_scope_changed')
    action = validate_next_action(NextAction.model_validate(selected['action']),view)
    async with worker_lock(key):
        saved=await ws._load(key+':run')
        state=await ws._load(key)
        if not saved or saved['id']!=run['id'] or state['engine']!=run['engine'] or saved['status']=='ended':
            raise ValueError('catalog_decision_owner_changed')
        if 'catalogPendingDecision' not in run:
            run['catalogPendingDecision']=selected
            # Charged after view validation. Count is committed along with the
            # selected action, so a crash cannot reset an accepted decision.
            run['catalogModelDecisions']=run.get('catalogModelDecisions',0)+int(selected['source']=='model')
            run['nodes'].append(dict(id=f"{run['id']}:decision:{run.get('catalogDecisionRevision',1)}",
                label='react_policy',outcome='completed',durationMs=selected['durationMs'],
                code='agent/app/control/react_decision.py:decide_next_action',
                detail={'decision':action.kind,'tool':action.tool_name,'modelCalled':selected['source']=='model',
                    'executionMode':'bounded_catalog_react','option':selected['optionId'],
                    'purpose':'根据本轮检索观察选择下一步；交易写入不在可选动作中。'}))
        run['pauseRequested']=saved.get('pauseRequested',False)
        if run['pauseRequested']:
            run.update(status='paused',notice='已保存下一步决策，等待继续。')
        elif action.kind=='ASK_CLARIFICATION':
            run.update(status='clarification',clarification={'catalog':True},catalogClarification=True,notice=action.question)
            question_id=f"{run['requestId']}:question:{run.get('catalogDecisionRevision',1)}"
            if not any(m['requestId']==question_id for m in state['messages']):
                state['messages'].append(dict(role='assistant',requestId=question_id,content=action.question,cards=[],source=run.get('source','web_unreviewed')))
                await ws._save(key,state)
        else:
            run['catalogPhase']=1 if action.kind=='CALL_TOOL' else 2
        run['revision']=saved['revision']+1
        await save_run(key,run)
    return run['status'] not in {'paused','clarification'}


async def product_work(key, run):
    """Reuse checkpoints while preserving the bound product presentation and TaskState."""
    from ..product_followup import verify_anchor, read_facts, answer_question, bind_plan
    from ..task_state import get_session_task_state
    plan=run['catalogPlan']
    anchor=plan.get('productContext')
    while run.get('catalogPhase',0)<len(PHASES):
        if run.get('catalogReact') and run['catalogPhase'] in {1,2}:
            if not await select_catalog_action(key,run):
                return
        phase=PHASES[run.get('catalogPhase',0)]
        began=time.perf_counter()
        if phase == 'publish':
            from .workspace_answer_stream import compose
            visible_answer = await compose(key, run, {'answer': run['catalogAnswer'], 'facts': run.get('productFacts', {})})
        if phase in {'prepare','publish'}:
            async with worker_lock(key):
                state=await ws._load(key)
                if state['engine']!=run['engine']:
                    raise ValueError('product_question_session_changed')
                verify_anchor(state,anchor)
                if anchor:
                    checked=await bind_plan(plan,state,await get_session_task_state(state['engine']),run['message'])
                    if not checked.get('productContext'):
                        raise ValueError('product_question_reference_expired')
                if phase=='prepare':
                    state['productFollowup']=deepcopy(anchor)
                    state['productQuestionRequest']=run['requestId']
                else:
                    if state.get('productQuestionRequest')!=run['requestId']:
                        raise ValueError('product_question_superseded')
                    if fingerprint(run.get('productFacts',{}))!=run['catalogEvidenceSha256']:
                        raise ValueError('product_question_evidence_changed')
                    if not any(m['role']=='assistant' and m['requestId']==run['requestId'] for m in state['messages']):
                        state['messages'].append({'role':'assistant','requestId':run['requestId'],
                            'content':visible_answer,'cards':[],'flow':deepcopy(run['nodes']),
                            'source':run.get('source','web_unreviewed')})
                    state['messages']=state['messages'][-60:]
                await ws._save(key,state)
        elif phase=='retrieve':
            run['productFacts']=await read_facts(plan)
            run['catalogEvidenceSha256']=fingerprint(run['productFacts'])
        elif phase=='answer':
            run['catalogAnswer'],run['catalogAnswerCall']=await answer_question(run['message'],plan,run['productFacts'])
        await checkpoint(key,run,phase=phase,seconds=time.perf_counter()-began)
        if run['status'] in {'paused','waiting','completed'}:
            return
