"""Owner-bound adapter for the existing durable runner and phase debugger.

Run records are separate from commerce state. Closing a tab never cancels a tool;
only a server-confirmed checkpoint may be resumed. No transaction JWT is passed.
"""
import asyncio
import hashlib
import json
import secrets
from typing import Literal

from fastapi import HTTPException, Request, Response, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from . import commerce_workspace as ws
from .commerce_evidence import evidence_view, concise_answer

BOOT = secrets.token_hex(16)
JOBS: dict[str, asyncio.Task] = {}
TERMINAL = {'completed', 'ended'}


def stopped_context_message(message, prior):
    return ('本轮用户需求（优先于历史）：' + message
            + '\n此前对话背景（冲突条件已被本轮修改）：'
            + prior[-(1900-len(message)):])


class Start(ws.ChatInput):
    mode: Literal['continuous', 'step'] = 'continuous'


class Command(BaseModel):
    model_config = ConfigDict(extra='forbid', populate_by_name=True)
    run_id: str = Field(alias='runId')
    revision: int = Field(ge=1)
    answer: str | None = Field(default=None, max_length=2000)
    request_id: str | None = Field(default=None, alias='requestId', min_length=1, max_length=100)


def public(run):
    if not run:
        return None
    return {k: run[k] for k in ('id', 'revision', 'mode', 'status', 'nodes', 'nextStage', 'notice', 'requestId', 'canResume') if k in run}


async def resumable(run):
    """An execution failure is not itself a durable recovery point."""
    if not run or run['status'] not in {'waiting', 'paused', 'interrupted', 'clarification', 'failed'}:
        return False
    if run.get('presentationData') or run.get('clarification'):
        return True
    if run.get('workflow') == 'catalog_workspace_v1':
        return isinstance(run.get('catalogPhase'), int) and run['catalogPhase'] < 4
    if run.get('mode') == 'step':
        return bool(run.get('debugId'))
    if run.get('recoveryBlocked') or not run.get('engine'):
        return False
    from ..task_state import get_session_task_state
    from ..graph import read_task_cursor
    state = await get_session_task_state(run['engine'])
    cursor = await read_task_cursor(state.task_id) if state else None
    marker = state.domain_state.get('v2RunMarker', {}) if state else {}
    receipt = state.domain_state.get('v2FinalAnswerReceipt', {}) if state else {}
    # Never resume an older run or an already finalized graph.
    return bool(cursor and cursor != run.get('initialCursor') and marker.get('runId')
                and marker.get('runId') != receipt.get('runId'))


async def refresh(key):
    run = await ws._load(key + ':run')
    if not run or run['status'] in TERMINAL:
        if run:
            run['canResume'] = False
        return run
    job = JOBS.get(key)
    if run['status'] in {'running', 'pausing'} and not (job and not job.done()):
        # This process no longer owns an active worker: require explicit recovery.
        run['status'] = 'interrupted'
        run['notice'] = '执行连接已中断。恢复将核对原检查点，不会重新发送整轮请求。'
    try:
        run['canResume'] = await resumable(run)
    except Exception:
        # An unavailable receipt store must not grant recovery authority.
        run['canResume'] = False
    return run


async def snapshot(key):
    state = await ws._load(key)
    run = await refresh(key)
    from .workspace_answer_stream import read
    return {**ws._public(state), 'run': public(run), 'answerStream': await read(key, run['id']) if run else None}


async def save_run(key, run):
    from ..workspace_archive import get_archive
    state = await ws._load(key)
    if state:
        await asyncio.to_thread(get_archive().record, key, state, kind='run', payload=run)
    await ws._save(key + ':run', run)


async def optional_pause_receipt(engine):
    from .. import main
    try:
        return await main.get_session_pause(engine)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        # A crash need not have been preceded by a user pause. The durable
        # restart path still validates task ownership, cursor and lease.
        return {}


def launch(key, run, operation='start', answer=None):
    task = asyncio.create_task(work(key, run, operation, answer))
    JOBS[key] = task
    def done(value):
        if JOBS.get(key) is value:
            JOBS.pop(key, None)
    task.add_done_callback(done)


async def publish(key, run, data):
    state = await ws._load(key)
    saved = await ws._load(key + ':run')
    if not saved or saved['id'] != run['id'] or saved['status'] == 'ended' or state['engine'] != run['engine']:
        raise HTTPException(409, '旧任务已经结束，不能覆盖当前对话')
    await archive_diagnostic(key, run, data)
    rows = (data.get('guideResult') or {}).get('products', [])
    ids = list(dict.fromkeys(int(row['product']['id']) for row in rows))[:6]
    cards = await asyncio.gather(*(ws._card(i) for i in ids))
    # Failed/replanned attempts remain visible as execution records, but may
    # never become product evidence. Reuse the FinalAnswer Validator whitelist.
    tool_nodes = evidence_view(data.get('tool_trace', []), [])
    validated = await published_evidence(run, data)
    evidence_view([dict(tool=r['tool'], ok=True, detail=r['evidence']) for r in validated], cards)
    detailed_tools = [n for n in run['nodes'] if n.get('kind') == 'tool' and n.get('input') is not None]
    run['nodes'].extend(n for n in tool_nodes if not any(t['label'] == n['label'] for t in detailed_tools))
    # Persist a sanitized message exactly once, including on final-outbox replay.
    if not any(m['role'] == 'assistant' and m['requestId'] == run['requestId'] for m in state['messages']):
        state['messages'].append(dict(role='assistant', requestId=run['requestId'],
                                      content=data.get('presentationAnswer') or concise_answer(data.get('answer', ''), cards), cards=cards,
                                      flow=run['nodes'], source=run.get('source', 'legacy_unverified')))
    state.update(cards=cards, selection=None, reference=data.get('referenceContext'))
    state['messages'] = state['messages'][-60:]
    await ws._save(key, state)


async def archive_diagnostic(key, run, data):
    from ..workspace_archive import get_archive
    from ..task_state import get_session_task_state
    from ..agent_trace import get_trace_store
    state = await ws._load(key)
    task = await get_session_task_state(run['engine'])
    trace = await get_trace_store().get(data['runId']) if data.get('runId') else None
    payload = dict(requestId=run['requestId'], engineSessionId=run['engine'],
        source=run.get('source', 'legacy_unverified'), mode=run.get('mode'),
        rawAnswer=data.get('answer'), runId=data.get('runId'),
        toolTrace=data.get('tool_trace', []), traceSummary=data.get('traceSummary'),
        trace=trace.model_dump(mode='json', by_alias=True) if trace and trace.session_id == run['engine'] else None,
        taskState=task.model_dump(mode='json', by_alias=True) if task and hasattr(task, 'model_dump') else None,
        labelingStatus='UNREVIEWED', benchmarkEligible=False)
    await asyncio.to_thread(get_archive().record, key, state, kind='diagnostic', payload=payload)


async def published_evidence(run, data):
    from ..task_state import get_session_task_state
    from ..harness import _build_validated_results
    from ..graph import read_terminal_response_receipt
    task = await get_session_task_state(run['engine'])
    if not task:
        return []
    if run.get('mode') == 'continuous':
        receipt = task.domain_state.get('v2FinalAnswerReceipt') or {}
        base = receipt.get('baseTaskRevision')
        if (not isinstance(base, int) or task.revision != base + 1 or
                receipt.get('finalizationRevision') != task.revision or
                receipt.get('runId') != data.get('runId') or
                receipt.get('answerSha256') != hashlib.sha256(data.get('answer', '').encode()).hexdigest()):
            return []
        verified = await read_terminal_response_receipt(task_id=task.task_id, run_id=receipt['runId'],
            thread_id=receipt['threadId'], proposal_hash=receipt['publicationId'], session_id=run['engine'], task_state=task)
        if not verified or verified['answer'] != data.get('answer'):
            return []
        # Finalization adds only the immutable answer receipt at base+1. Project
        # the already validated base revision in memory; never modify stored state.
        task = task.model_copy(update={'revision': base})
    return _build_validated_results(task, [])


async def work(key, run, operation, answer):
    if run.get('workflow') == 'catalog_workspace_v1':
        from .catalog_workspace import work as catalog_work
        return await catalog_work(key, run, operation, answer)
    from .. import main
    try:
        if run['mode'] == 'step':
            turn = await main.read_debug_turn(run['debugId'])
            if operation == 'step':
                turn = await main.step_debug_turn(turn.debug_turn_id,
                    main.DebugTurnStepRequest(expectedRevision=run['debugRevision']))
            run['debugRevision'] = turn.revision
            from ..execution_view import execution_nodes, safe_value
            run['nodes'] = [dict(label=s.label, outcome=s.outcome, durationMs=s.duration_ms,
                                 id=f'{turn.debug_turn_id}:{s.index}', finishedAt=s.finished_at,
                                 cause={'requestId': run['requestId'], 'description': '单步调试器完成此检查点'},
                                 output=safe_value(s.key_state),
                                 detail={k: v for k, v in s.key_state.items() if k in {
                                     'action', 'tool', 'toolOk', 'toolDurationMs', 'taskRevision',
                                     'contextTokenCount', 'allowedToolNames', 'publishedGuideResult'}},
                                 code=f'{s.code_file}:{s.code_function}', error=s.error_code) for s in turn.steps]
            try:
                run['nodes'].extend(execution_nodes(await main._debug_load_task(turn), run))
            except Exception:
                run['nodes'].append(dict(label='执行明细读取', outcome='unavailable',
                    detail={'purpose': '明细暂不可用，不改变已经完成的步骤结果。'}))
            run['nextStage'] = turn.next_stage
            run['status'] = 'waiting' if turn.status in {'queued', 'running'} else ('completed' if turn.status == 'completed' else 'failed')
            if any(s.key_state.get('errorCode') or s.key_state.get('plannerOutcome') == 'planning_failed' for s in turn.steps):
                run.update(status='failed', notice='该步骤未通过校验，已停止；请查看节点错误或结束本轮。')
            if run['status'] == 'completed':
                from ..harness import _build_validated_results
                state = await main._debug_load_task(turn)
                validated = _build_validated_results(state, [])
                traces = [dict(tool=r['tool'], ok=True, detail=r['evidence']) for r in validated]
                async with ws._lock(key):
                    await publish(key, run, dict(answer=turn.final_answer or '', guideResult=turn.guide_result, tool_trace=traces))
                run['notice'] = '单步执行已结束'
        else:
            kwargs = dict(message=run.get('contextualMessage') or run['message'], sessionId=run['engine'], domainHint='ecommerce')
            if operation == 'continue' and not run.get('presentationData'):
                if run.get('clarification'):
                    if not answer:
                        raise HTTPException(409, '请先回答澄清问题')
                    kwargs['resume'] = {**run['clarification'], 'answer': answer}
                else:
                    from ..task_state import get_session_task_state
                    from ..graph import read_task_cursor
                    state = await get_session_task_state(run['engine'])
                    cursor = await read_task_cursor(state.task_id) if state else None
                    if not state or not cursor or cursor == run.get('initialCursor'):
                        raise HTTPException(409, '尚无本轮可恢复检查点；请结束本轮后重新提问')
                    kwargs['restartTaskId'] = state.task_id
                    # Fetch server-owned receipt, never accept arbitrary browser task/session ids.
                    receipt = await optional_pause_receipt(run['engine'])
                    if receipt.get('state') in {'paused', 'resuming'}:
                        kwargs['pauseReceipt'] = {**receipt, 'state': 'paused'}
            elif run.get('reference'):
                kwargs['referenceContext'] = run['reference']
            data = run.get('presentationData') or (await main.chat_llm_durable(main.DurableEcommerceChatRequest(**kwargs),
                        authorization=None, shopping_memory_session=None)).model_dump(by_alias=True, mode='json')
            if (data.get('trace') or {}).get('status') != 'ok':
                raise RuntimeError('durable_request_failed')
            summary = data.get('traceSummary') or {}
            run.setdefault('traces', []).extend(data.get('tool_trace', []))
            data['tool_trace'] = run['traces']
            await archive_diagnostic(key, run, data)
            run['nodes'] = await lifecycle_nodes(data, run)
            if (summary.get('checkpointPause') or {}).get('state') == 'paused':
                run.update(status='paused', notice='已在安全检查点暂停。继续会恢复原待执行节点。')
            elif summary.get('durableResume'):
                run.update(status='clarification', clarification=summary['durableResume'], notice=data['answer'])
                async with ws._lock(key):
                    state = await ws._load(key)
                    question_id = run['requestId'] + ':question:' + hashlib.sha256(json.dumps(summary['durableResume'], sort_keys=True).encode()).hexdigest()[:16]
                    if not any(m['requestId'] == question_id for m in state['messages']):
                        state['messages'].append(dict(role='assistant', requestId=question_id, content=data['answer'], cards=[], source=run.get('source', 'web_unreviewed')))
                        await ws._save(key, state)
            elif summary.get('finalAction') in {'resume_rejected', 'safe_stop', 'failed'}:
                run.update(status='failed', recoveryBlocked=not bool(summary.get('checkpointPause')),
                    notice='本轮需求处理未完成，尚未执行新的检索。可以修改问题或新建对话。'
                    if summary.get('failureCode') == 'task_state_update_failed'
                    else '执行已安全停止；请查看步骤结果，或发送新问题。')
            else:
                run.pop('clarification', None)
                fact_failure = next((t.get('detail') for t in data.get('tool_trace', [])
                    if not t.get('ok') and isinstance(t.get('detail'), dict)
                    and t['detail'].get('code') == 'authoritative_product_facts_unavailable'), None)
                # Save original signed result before streaming a separate presentation.
                # Recovery here must never run the search/transaction workflow again.
                from .workspace_answer_stream import compose
                async with ws._lock(key):
                    previous = await ws._load(key + ':run')
                    run.update(presentationData=data, revision=(previous or run)['revision']+1)
                    await save_run(key, run)
                displayed = (data.get('guideResult') or {}).get('products', [])[:6]
                if fact_failure:
                    # A dependency outage is not an empty search. Do not ask a
                    # presentation model to paraphrase away this distinction.
                    data['presentationAnswer'] = '已检索到候选商品，但获取商品详情或库存时后台服务暂不可用，未能完成核验。这不代表没有相关商品或已经售罄。你可以稍后重新查询，或继续提出其他需求。'
                else:
                    data['presentationAnswer'] = await compose(key, run, {'answer': data.get('answer', ''),
                        'displayOrder': [{'number': i+1, 'product': row['product']} for i, row in enumerate(displayed)]})
                async with ws._lock(key):
                    await publish(key, run, data)
                run.update(status='completed', notice='本轮已完成')
        run['revision'] += 1
        async with ws._lock(key):
            previous = await ws._load(key + ':run')
            if not previous or previous['id'] != run['id'] or previous['status'] == 'ended':
                return
            run['revision'] = max(run['revision'], (previous or {}).get('revision', 0) + 1)
            await save_run(key, run)
    except Exception as exc:
        from .workspace_answer_stream import AnswerPaused
        ws.logger.warning('Workspace control incomplete: %s', type(exc).__name__)
        run.update(status='interrupted', notice=(exc.detail if isinstance(exc, HTTPException) and isinstance(exc.detail, str)
                   else '本轮尚未完成；请恢复原任务，或结束本轮。不会自动提交交易。'))
        if isinstance(exc, AnswerPaused):
            run.update(status='paused', notice='已停止输出并保存文字。继续会接着生成，不重做搜索或交易。')
        run['revision'] += 1
        async with ws._lock(key):
            previous = await ws._load(key + ':run')
            if not previous or previous['id'] != run['id'] or previous['status'] == 'ended':
                return
            run['revision'] = max(run['revision'], (previous or {}).get('revision', 0) + 1)
            await save_run(key, run)


async def lifecycle_nodes(data, run):
    from ..agent_trace import get_trace_store
    from ..execution_view import execution_nodes, safe_value, source_reference
    from ..task_state import get_session_task_state
    trace = await get_trace_store().get(data['runId']) if data.get('runId') else None
    nodes = list(run.get('nodes', []))
    if trace and trace.session_id == run['engine']:
        for phase in trace.phases:
            detail = safe_value(phase.detail or {})
            if phase.task_revision is not None:
                detail['taskRevision'] = phase.task_revision
            node = dict(label=phase.phase, outcome=phase.outcome, durationMs=phase.duration_ms,
                        detail=detail, startedAt=phase.started_at, finishedAt=phase.finished_at,
                        source=source_reference(phase.phase),
                        code=phase.code_location, cause={'requestId': run['requestId'],
                        'description': '此请求的实际阶段记录；阶段耗时可能包含嵌套工具调用。'})
            if node not in nodes:
                nodes.append(node)
    try:
        task = await get_session_task_state(run['engine'])
        for node in execution_nodes(task, run):
            if not any(n.get('id') == node['id'] for n in nodes):
                nodes.append(node)
    except Exception:
        nodes.append(dict(label='执行明细读取', outcome='unavailable',
            detail={'purpose': '明细暂不可用，不改变已经完成的步骤结果。'}))
    return nodes


@ws.router.get('/control')
async def control(request: Request, response: Response):
    key, _, _ = await ws._identity(request, response)
    return await snapshot(key)


async def stream_snapshot(key, run_id):
    """Read the existing job, never launch/resume it or publish an unverified answer."""
    run = await refresh(key)
    if not run or run['id'] != run_id:
        raise HTTPException(409, '本轮任务已变化，请恢复页面查看最新状态')
    # Read state AFTER the terminal receipt: publish saves the answer first.
    state = await ws._load(key)
    if not state:
        raise HTTPException(409, '购物空间已变化，请恢复页面')
    view = public(run)
    if run['status'] in {'running', 'pausing'} and run.get('workflow') != 'catalog_workspace_v1':
        # Project only new durable Executor receipts, using the existing
        # whitelist and initialStepKeys boundary. Never persist this UI view.
        from ..execution_view import execution_nodes
        from ..task_state import get_session_task_state
        try:
            task = await get_session_task_state(run['engine'])
            nodes = list(view.get('nodes', []))
            known = {n.get('id') for n in nodes if n.get('id')}
            nodes.extend(n for n in execution_nodes(task, run) if n.get('id') not in known)
            view = {**view, 'nodes': nodes}
        except Exception:
            # Missing diagnostics do not change the actual runner status.
            pass
    from .workspace_answer_stream import read
    return {**ws._public(state), 'run': view, 'answerStream': await read(key, run_id) if run['status'] not in TERMINAL else None}


STREAM_INTERVAL = 0.1
STREAM_LIFETIME = 45.0
STREAM_HEARTBEAT = 10.0


async def run_events(request, key, run_id):
    """SSE latest-state subscription; reconnect resends a full snapshot.

    The durable runner owns work. Disconnecting this generator cannot cancel
    its JOBS task. Presentation content is streamed; reasoning/tool JSON is not.
    """
    clock = asyncio.get_running_loop().time
    started = last_heartbeat = clock()
    previous = None
    previous_answer = ''
    previous_sequence = -1
    while clock() - started < STREAM_LIFETIME:
        if await request.is_disconnected():
            return
        try:
            # Recheck session expiry/revocation and CSRF on a long connection.
            current_key, _, _ = await ws._identity(request, Response())
            if current_key != key:
                raise HTTPException(403, '会话身份已变化')
            value = await stream_snapshot(key, run_id)
            answer = value.get('answerStream')
            if answer and answer['sequence'] != previous_sequence:
                # First frame on reconnect is an authoritative prefix. Subsequent
                # frames are actual newly received provider text, never timed slices.
                append = previous_sequence >= 0 and answer['text'].startswith(previous_answer)
                event = dict(type='answer_delta', runId=run_id, requestId=answer['requestId'],
                             sequence=answer['sequence'], replace=not append,
                             text=answer['text'][len(previous_answer):] if append else answer['text'],
                             status=answer['status'])
                yield 'event: answer_delta\ndata: ' + json.dumps(event, ensure_ascii=False) + '\n\n'
                previous_answer, previous_sequence = answer['text'], answer['sequence']
            payload = json.dumps({'type': 'snapshot', 'workspace': value}, ensure_ascii=False, separators=(',', ':'))
            # Do not resend the entire conversation for every token. A reconnect
            # or a state transition still includes the authoritative full prefix.
            stable = {k: v for k, v in value.items() if k != 'answerStream'}
            digest = hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            if digest != previous:
                yield f'id: {digest}\nevent: snapshot\ndata: {payload}\n\n'
                previous = digest
            if value['run']['status'] not in {'running', 'pausing'}:
                yield 'event: settled\ndata: {"type":"settled"}\n\n'
                return
            if clock() - last_heartbeat >= STREAM_HEARTBEAT:
                yield ': keep-alive\n\n'
                last_heartbeat = clock()
        except HTTPException as exc:
            yield 'event: error\ndata: ' + json.dumps({'type': 'error', 'status': exc.status_code,
                'message': '会话或任务状态已变化，请重新恢复页面。'}, ensure_ascii=False) + '\n\n'
            return
        except Exception:
            yield 'event: error\ndata: {"type":"error","status":503,"message":"进度连接暂不可用；任务不会因此取消。"}\n\n'
            return
        await asyncio.sleep(STREAM_INTERVAL)
    # Planned connection rotation is not a runner failure or a network error.
    yield 'event: reconnect\ndata: {"type":"reconnect"}\n\n'


@ws.router.get('/control/events')
async def control_events(request: Request, response: Response, runId: str = Query(min_length=1, max_length=80)):
    key, _, _ = await ws._identity(request, response)
    run = await refresh(key)
    if not run or run['id'] != runId:
        raise HTTPException(409, '本轮任务已变化，请恢复页面查看最新状态')
    return StreamingResponse(run_events(request, key, runId), media_type='text/event-stream',
        headers={'Cache-Control': 'no-store, no-transform', 'X-Accel-Buffering': 'no'})


@ws.router.post('/run')
async def start(body: Start, request: Request, response: Response):
    key, state, _ = await ws._identity(request, response)
    async with ws._lock(key):
        current = await refresh(key)
        if current and current['status'] not in TERMINAL:
            if current['requestId'] == body.request_id and current['message'] == body.message and current['mode'] == body.mode:
                return await snapshot(key)
            raise HTTPException(409, '请先继续或结束当前任务，再修改需求或调试模式')
        state = await ws._load(key)
        if any(m.get('requestId') == body.request_id for m in state['messages']):
            raise HTTPException(409, '该请求已受理，请恢复页面查看结果')
        if state.get('checkout') and state['checkout'].get('pending'):
            raise HTTPException(409, '请先回查未决交易')
        from .. import main
        from ..task_state import get_session_task_state
        from ..graph import read_task_cursor
        old = await get_session_task_state(state['engine'])
        initial = await read_task_cursor(old.task_id) if old else None
        run = dict(id=secrets.token_hex(16), requestId=body.request_id, revision=1, mode=body.mode,
                   status='running', message=body.message, engine=state['engine'], nodes=[], boot=BOOT,
                   initialCursor=initial, source='automated_test' if request.headers.get('X-Conversation-Source') == 'automated_test' else 'web_unreviewed')
        prior = state.pop('stoppedConversationContext', '')
        if prior and len(body.message) < 1900:
            # Literal extractors read the first explicit constraint. Keep the
            # newest request first; history must not overwrite its budget.
            run['contextualMessage'] = stopped_context_message(body.message, prior)
        from ..execution_view import execution_key
        run['initialStepKeys'] = [execution_key(r) for r in (old.domain_state.get('stepExecutionResults', []) if old else []) if isinstance(r, dict)]
        ref = state.get('reference')
        if ref and ref.get('handle'):
            run['reference'] = dict(handle=ref['handle'], presentationMode='compact')
        if ws.auth.settings.catalog_workspace_enabled:
            from ..catalog_conversation import plan_turn
            plan, plan_call = await plan_turn(body.message, state)
            from ..product_followup import bind_plan
            plan = await bind_plan(plan, state, old, body.message)
            run['catalogRouteCall'] = plan_call
            if plan['route'] in {'catalog', 'product'}:
                from ..catalog_service import workflow_code_binding
                run.update(workflow='catalog_workspace_v1', catalogPlan=plan, catalogPhase=0,
                    catalogReact=ws.auth.settings.agent_control_runtime=='react_v1' and ws.auth.settings.agent_react_live_enabled,
                    catalogCodeBinding=workflow_code_binding(),
                    catalogBaseRevision=(state.get('catalogSearch') or {}).get('revision', 0),
                    nextStage='整理当前需求', pauseRequested=False)
                run.pop('reference', None)
                if body.mode == 'step':
                    run.update(status='waiting', notice='商品搜索已准备，可以逐步执行。')
            elif state.get('catalogSearch'):
                # Search-document references never cross into transaction state.
                state.pop('catalogSearch', None)
                state.pop('catalogPendingRequest', None)
                state.update(cards=[], selection=None, reference=None, engine='web-' + secrets.token_urlsafe(24))
                run.update(engine=state['engine'], initialCursor=None, initialStepKeys=[])
                run.pop('reference', None)
            if plan['route']=='business':
                state.pop('productFollowup', None)
                if state.pop('restoredConversation', False):
                    # The router has the saved user turns and must restate the
                    # current request. Do not revive an old CandidateScope,
                    # checkout confirmation, cursor, or failed executor plan.
                    query = plan.get('query', '').strip()
                    if query:
                        run['contextualMessage'] = query
        if body.mode == 'step' and run.get('workflow') != 'catalog_workspace_v1':
            if len(body.message) > 500:
                raise HTTPException(422, '单步调试每轮最多 500 字')
            turn = await main.create_debug_turn(main.DebugTurnCreateRequest(message=body.message, sessionId=state['engine'], domainHint='ecommerce'))
            run.update(debugId=turn.debug_turn_id, debugRevision=turn.revision, status='waiting', nextStage=turn.next_stage)
        state['messages'].append(dict(role='user', content=body.message, requestId=body.request_id,
            source=run['source'], submittedAt=ws.datetime.now(ws.timezone.utc).isoformat()))
        state['checkout'] = None
        await ws._save(key, state)
        await save_run(key, run)
        if body.mode == 'continuous':
            launch(key, run)
        return await snapshot(key)


@ws.router.post('/control/{operation}')
async def command(operation: Literal['pause', 'continue', 'step', 'end'], body: Command, request: Request, response: Response):
    key, state, _ = await ws._identity(request, response)
    from .catalog_workspace import worker_lock
    # A stop request must not lose a race against a short token-persistence lock.
    # Other mutation commands keep the original immediate-conflict contract.
    async with (worker_lock(key) if operation == 'pause' else ws._lock(key)):
        run = await refresh(key)
        if not run or run['id'] != body.run_id or (run['revision'] != body.revision and
                not (operation == 'pause' and body.revision <= run['revision'])):
            raise HTTPException(409, '控制状态已更新，请刷新后重试')
        if operation == 'pause' and run['status'] in {'pausing','paused','completed','ended'}:
            return await snapshot(key)
        from .. import main
        if operation == 'pause':
            if run['mode'] != 'continuous' or run['status'] not in {'running', 'pausing'}:
                raise HTTPException(409, '当前任务不在连续执行中')
            # The runner must acknowledge a checkpoint; no browser AbortController.
            if run.get('workflow') == 'catalog_workspace_v1' or run.get('presentationData'):
                run['pauseRequested'] = True
            else:
                await main.pause_session_run(run['engine'])
            run.update(status='pausing', notice='正在暂停：等待当前工具结束并保存安全检查点…')
        elif operation == 'end':
            if run['status'] in {'running', 'pausing'}:
                raise HTTPException(409, '请先安全暂停，再结束本轮')
            if run['mode'] == 'step' and run.get('workflow') != 'catalog_workspace_v1':
                turn = await main.read_debug_turn(run['debugId'])
                await main.cancel_debug_turn(turn.debug_turn_id, main.DebugTurnStepRequest(expectedRevision=turn.revision))
            # Isolate abandoned checkpoint from the next task without deleting its receipts.
            state = await ws._load(key)
            from .workspace_answer_stream import read
            partial = await read(key, run['id'])
            if partial and partial['text'] and not any(m['role'] == 'assistant' and m['requestId'] == run['requestId'] for m in state['messages']):
                state['messages'].append(dict(role='assistant', requestId=run['requestId'],
                    content=partial['text'] + '\n\n*已停止生成*', cards=[], flow=run['nodes']))
            state.update(engine='web-' + secrets.token_urlsafe(24), reference=None)
            state['stoppedConversationContext'] = '\n'.join(m['content'] for m in state['messages'] if m['role']=='user')[-1500:]
            if run.get('workflow') == 'catalog_workspace_v1':
                # Preserve the user's requirements, not abandoned execution
                # authority. The next run revalidates/retrieves its own scope.
                state.pop('catalogPendingRequest', None)
                state.update(cards=[], selection=None)
            await ws._save(key, state)
            run.update(status='ended', notice='本轮已结束，历史执行记录保留。可以提出新需求。')
        else:
            if run['status'] not in {'waiting', 'paused', 'interrupted', 'clarification', 'failed'}:
                raise HTTPException(409, '当前状态不能继续')
            if operation == 'continue' and run['mode'] == 'continuous' and not await resumable(run):
                raise HTTPException(409, '本轮没有可恢复检查点；请发送新问题或新建对话')
            if operation == 'step' and run['mode'] != 'step':
                raise HTTPException(409, '不能在本轮中切换执行模式')
            if run.get('clarification') and not (body.answer and body.answer.strip()):
                raise HTTPException(409, '请回答澄清问题')
            if run.get('clarification') and body.answer:
                state = await ws._load(key)
                answer_id = body.request_id or f"{run['requestId']}:reply:{run['revision']}"
                if any(m['requestId'] == answer_id for m in state['messages']):
                    raise HTTPException(409, '该补充消息已受理，请恢复页面查看结果')
                state['messages'].append(dict(role='user', requestId=answer_id, content=body.answer,
                    source=run.get('source', 'web_unreviewed'), submittedAt=ws.datetime.now(ws.timezone.utc).isoformat()))
                await ws._save(key, state)
                run['replyRequestId'] = answer_id
            recovering_step = run['mode'] == 'step' and run['status'] == 'interrupted'
            run.update(status='running', notice='正在核对并继续原任务…')
            if run.get('workflow') == 'catalog_workspace_v1' or run.get('presentationData'):
                run['pauseRequested'] = False
            run['revision'] += 1
            await save_run(key, run)
            launch(key, run, 'step' if operation == 'step' and not recovering_step else 'continue', body.answer)
            return await snapshot(key)
        run['revision'] += 1
        await save_run(key, run)
        return await snapshot(key)
