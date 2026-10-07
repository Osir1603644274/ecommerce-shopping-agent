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
from ..catalog_service import fingerprint, workflow_code_binding, verify_scope
from ..guide_execution import answer as answer_catalog, retrieve as retrieve_catalog, select_provider_query

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
            metrics = {}
            if phase == 'prepare':
                receipt = run.get('catalogRouteCall') or {}
                metrics = {'parseAttempts': receipt.get('parseAttempts', 1),
                           'parseDurationMs': receipt.get('durationMs'),
                           'parseUsage': receipt.get('usage')}
            elif phase == 'answer':
                receipt = run.get('catalogAnswerCall') or {}
                metrics = {'answerDurationMs': receipt.get('durationMs'),
                           'answerUsage': receipt.get('usage')}
            if public_fields.get('detail'):
                public_fields['detail'] = {**public_fields['detail'], **metrics}
            run['nodes'].append({'id': run['id']+':catalog:'+phase+(f":{len(run['nodes'])}" if run.get('catalogReact') else ''), 'label': labels[phase],
                'outcome': 'completed', 'durationMs': seconds*1000,
                'code': 'agent/app/api/catalog_workspace.py:work',
                'detail': {'workflow': 'product_question_v1' if run['catalogPlan']['route']=='product' else 'catalog_workspace_v1', 'phase': phase,
                    'scopeId': (run.get('catalogNext', {}).get('scope') or {}).get('scopeId'), **metrics}, **public_fields})
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
            from ..guide_interpreter import GuideInterpretationError, GuideRevisionConflictError, interpret_and_commit
            from ..task_state import get_session_task_state
            state = await ws._load(key)
            task = await get_session_task_state(state['engine'])
            if task is None:
                raise ValueError('guide_clarification_task_expired')
            try:
                task, plan, receipt = await interpret_and_commit(
                    answer, task, workspace=state, turn_id=run['replyRequestId'],
                )
            except GuideRevisionConflictError as exc:
                raise HTTPException(409, '导购状态刚被另一轮更新，请刷新后重试') from exc
            except GuideInterpretationError as exc:
                raise HTTPException(422, '本轮补充信息未能通过结构化校验，请换种说法再试') from exc
            run.update(message=answer, requestId=run['replyRequestId'], catalogPlan=plan,
                intent=plan['intent'], catalogRouteCall=receipt, catalogPhase=0,
                guideTaskId=task.task_id, guideTaskRevision=task.revision,
                catalogModelDecisions=0, catalogQueries=[])
            for field in ('catalogClarification','clarification','catalogPendingDecision','catalogNext','catalogBefore','catalogNotice',
                          'presentationData','productFacts','catalogEvidenceSha256','catalogProviderQuery','catalogProviderQueryReason'):
                run.pop(field,None)
            if plan['route']=='business':
                # Start a server-bound business task; never fabricate a durable resume.
                run.pop('workflow',None)
                async with worker_lock(key):
                    state.update(cards=[],selection=None,reference=None)
                    state.pop('catalogSearch',None)
                    state.pop('catalogScope',None)
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
                    old_scope = state.get('catalogScope') or (state.get('catalogSearch') or {}).get('scope')
                    replay_prepare = state.get('catalogPendingRequest')==run['requestId']
                    from ..task_state import get_task_state
                    from ..guide_state import ShoppingState, catalog_projection
                    task = await get_task_state(run['guideTaskId'])
                    if not task or task.revision != run['guideTaskRevision'] or task.session_id != run['engine']:
                        raise HTTPException(409, '导购状态版本已变化，请恢复页面核对')
                    decision = task.domain_state.get('guideTurnDecision') or {}
                    if decision.get('turnId') != run['requestId']:
                        raise HTTPException(409, '本轮导购决策已失效')
                    guide = ShoppingState.model_validate(task.domain_state['shopping'])
                    if 'catalogBefore' not in run:
                        run['catalogBefore'] = {'query': guide.query,
                            'requirements': catalog_projection(guide)['requirements']}
                    old_scope = old_scope if (plan['action'] in {'compare', 'clarify', 'inspect'}
                        or plan['action']=='undo' and not decision.get('semanticChanged')) else None
                    if old_scope:
                        ref = task.domain_state.get('guideEvidenceRef') or {}
                        verify_scope(old_scope)
                        if (ref.get('taskId') != task.task_id or ref.get('scopeId') != old_scope.get('scopeId')
                                or ref.get('scopeSha256') != fingerprint(old_scope)):
                            old_scope = None
                    next_state = catalog_projection(guide, scope=old_scope, revision=task.revision)
                    notice = (state.get('catalogPendingNotice') if replay_prepare else
                        '没有可以撤销的需求修改。' if plan['action']=='undo'
                        and not decision.get('semanticChanged') else None)
                    run['catalogNext'] = next_state
                    if notice:
                        run['catalogNotice'] = notice
                    # Invalidate references immediately for a changed query.
                    # The pending state is identified by this request on retry.
                    if decision.get('semanticChanged'):
                        state.update(cards=[], selection=None, reference=None)
                        state.pop('catalogScope', None)
                        state.pop('productFollowup', None)
                    # A legacy demand may be read for migration once, never written again.
                    state.pop('catalogSearch', None)
                    state['catalogPendingRequest'] = run['requestId']
                    state['catalogPendingNotice'] = notice
                    await ws._save(key, state)
            elif phase=='retrieve':
                if (plan['action'] in {'search', 'refine', 'new', 'undo'} and not run.get('catalogNotice')
                        and run['catalogNext'].get('query')):
                    current=run['catalogNext']
                    retrieval_query, query_reason=select_provider_query(current, run.get('catalogPendingDecision'))
                    run['catalogProviderQuery'] = retrieval_query
                    run['catalogProviderQueryReason'] = query_reason
                    run['catalogNext']['scope'] = await retrieve_catalog(
                        current['query'], retrieval_query, current['requirements'])
                    if run.get('catalogReact'):
                        run.setdefault('catalogQueries',[]).append(retrieval_query)
                scope = run['catalogNext'].get('scope')
                verify_scope(scope)
                run['catalogEvidenceSha256'] = fingerprint(scope)
            elif phase=='answer':
                if run.get('catalogNotice'):
                    run['catalogAnswer'] = run['catalogNotice']
                elif plan['action']=='undo' and not run['catalogNext']['query']:
                    run['catalogAnswer'] = '已撤销上次需求修改，当前没有正在检索的商品需求。'
                    run['catalogAnswerCall'] = None
                else:
                    text, receipt, scope = await answer_catalog(run['message'], plan, run['catalogNext'])
                    if (receipt or {}).get('scopeReview'):
                        if fingerprint(run['catalogNext']['scope'])!=run['catalogEvidenceSha256']:
                            raise ValueError('catalog_review_evidence_changed')
                        run['catalogRetrievedEvidenceSha256'] = run['catalogEvidenceSha256']
                    run['catalogNext']['scope'] = scope
                    run['catalogEvidenceSha256'] = fingerprint(scope)
                    run['catalogAnswer'] = text
                    run['catalogAnswerCall'] = receipt
            else:
                from ..catalog_commerce import resolve_optional_cards, source_only_answer
                scope = run['catalogNext'].get('scope')
                noop_undo = plan['action']=='undo' and bool(run.get('catalogNotice'))
                if scope and not noop_undo:
                    from ..task_state import get_task_state
                    from ..guide_evidence import publish_scope
                    task = await get_task_state(run['guideTaskId'])
                    if not task or task.session_id != run['engine']:
                        raise ValueError('guide_evidence_task_changed')
                    ref = task.domain_state.get('guideEvidenceRef') or {}
                    if ref.get('runId') == run['id'] and ref.get('scopeSha256') == fingerprint(scope):
                        run['guideTaskRevision'] = task.revision
                    elif task.revision == run['guideTaskRevision']:
                        task = await publish_scope(task, scope, run_id=run['id'])
                        run['guideTaskRevision'] = task.revision
                    else:
                        raise ValueError('guide_evidence_task_revision_changed')
                # Perform read-only authority resolution before entering the publication lock.
                # Retry may refresh prices, but never grants order-creation authority itself.
                trading_cards, authority_notice = ([], None) if noop_undo else await resolve_optional_cards(scope)
                if authority_notice:
                    visible_answer = source_only_answer(scope, authority_notice)
                else:
                    visible_answer = run['catalogAnswer']
                if not noop_undo:
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
                    if not noop_undo:
                        state.update(catalogScope=scope, cards=trading_cards, selection=None, reference=None)
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
    view, query_bindings = decision_view(validation_run)
    if selected['viewHash'] != view.decision_view_hash:
        raise ValueError('catalog_decision_scope_changed')
    action = validate_next_action(NextAction.model_validate(selected['action']),view)
    expected_query = query_bindings.get((action.argument_refs or {}).get('query'))
    if selected.get('query') != expected_query:
        raise ValueError('catalog_decision_query_binding_changed')
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
            visible_answer = run['catalogAnswer']
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
