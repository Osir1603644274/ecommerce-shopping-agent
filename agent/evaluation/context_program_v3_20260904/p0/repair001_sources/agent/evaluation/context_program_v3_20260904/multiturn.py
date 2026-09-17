"""P4 owned-Redis, real runtime Context A/B with complete provider receipts."""
import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import time

from .common import HERE, ROOT, RecordedClient, append, file_sha, freeze, check_freeze, json_new, now, rows, sha

ARMS = ('RAW_FULL_CONTROL', 'CONTEXT_TREATMENT')


@contextmanager
def owned_redis(out):
    import redis
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    runtime = out / 'redis'; runtime.mkdir()
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    with (out / 'redis.log').open('x', encoding='utf-8') as log:
        server = subprocess.Popen([r'C:\Program Files\Redis\redis-server.exe', '--bind', '127.0.0.1',
            '--port', str(port), '--save', '', '--appendonly', 'no', '--dir', str(runtime)],
            stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
        try:
            client = redis.Redis(host='127.0.0.1', port=port, socket_timeout=1)
            for _ in range(30):
                try:
                    if client.ping(): break
                except redis.RedisError: time.sleep(.2)
            else: raise RuntimeError('owned_redis_not_ready')
            if client.dbsize() != 0: raise RuntimeError('owned_redis_not_empty')
            json_new(out / 'redis_identity.json', {'pid': server.pid, 'port': port, 'sharedServiceTouched': False})
            os.environ['REDIS_URL'] = f'redis://127.0.0.1:{port}/0'
            yield
        finally:
            if server.poll() is None:
                server.terminate()
                try: server.wait(timeout=10)
                except subprocess.TimeoutExpired: server.kill(); server.wait()


async def run(out, data, selected_strict):
    # Import runtime only after the isolated Redis URL is set.
    from openai import AsyncOpenAI
    from agent.app import llm
    from agent.app.evaluation_context_arm import issue_evaluation_context_arm
    from agent.evaluation.real_user_multiturn_ab_executor_20260902_v2 import lane_runtime as old
    from agent.evaluation.real_user_multiturn_ab_executor_20260902_v1.runner import LaneBinding
    from agent.evaluation.context_program_v1_20260904.model_forced_multiturn import build_schedule
    settings = llm.settings
    settings.multi_agent_v2_enabled = False
    settings.memory_projection_client_enabled = False
    settings.task_state_extraction_strict_enabled = selected_strict
    settings.deepseek_base_url = 'https://api.deepseek.com/beta'
    assert settings.redis_url == os.environ['REDIS_URL']
    if file_sha(old.CATALOG_PATH) != old.EXPECTED_CATALOG_SHA256: raise RuntimeError('catalog_drift')
    products = tuple(old._product_from_catalog(r) for r in old._read_jsonl(old.CATALOG_PATH))
    if len(products) != old.EXPECTED_PRODUCTS: raise RuntimeError('catalog_size')
    schedule = build_schedule(data)
    json_new(out / 'schedule.json', schedule)
    results = []
    async with AsyncOpenAI(api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url,
                           timeout=45, max_retries=0) as provider:
        client = RecordedClient(provider, phase='P4', output=out)
        # Reuse tested isolation/storage and catalog logic, NOT its obsolete
        # FrozenModelClient which silently forces max_tokens=1024.
        runtime = old.IsolatedLaneRuntime.__new__(old.IsolatedLaneRuntime)
        runtime._namespace = 'ctxv3-' + out.name
        runtime._ids = {}
        runtime._transport = old.FrozenCatalogTransport(products)
        runtime._client = client
        try:
            for conversation in data:
                cid = conversation['conversationId']
                lanes = await runtime.begin_conversation_async(conversation, ARMS)
                assert len({lane.branch_point_state_hash for lane in lanes.values()}) == 1
                dialogue = {arm: [] for arm in ARMS}
                for row in [r for r in schedule if r['conversationId'] == cid]:
                    arm = row['arm']; base = lanes[arm]
                    lane = LaneBinding(runtime.turn_run_id(row, base), base.task_id, base.session_id,
                                       base.branch_point_state_hash, base.pre_state_revision)
                    state = await runtime.load_task_state(row, lane)
                    assert (state.task_id, state.session_id, state.revision) == (lane.task_id, lane.session_id, lane.pre_state_revision)
                    client.binding = {'attempt': out.name, 'conversationId': cid, 'turnId': row['turnId'],
                        'arm': arm, 'runId': lane.run_id, 'taskId': lane.task_id, 'sessionId': lane.session_id}
                    capability = issue_evaluation_context_arm(arm=arm, run_id=lane.run_id,
                        task_id=lane.task_id, session_id=lane.session_id, model=settings.deepseek_model,
                        model_client=client, tool_transport=runtime._transport, provider_max_retries=0)
                    final_state = state
                    async def capture(current, _phase):
                        nonlocal final_state
                        final_state = current
                        await runtime.persist_task_state(row, lane, current)
                    prior = [dict(x) for x in dialogue[arm]] if arm == ARMS[0] else None
                    record = {**client.binding, 'executionOrdinal': row['executionOrdinal'],
                        'inputMessageSha256': row['inputMessageSha256'], 'preStateRevision': state.revision,
                        'branchPointStateHash': lane.branch_point_state_hash, 'historyMode': row['historyMode'],
                        'rawPriorMessageCount': len(prior or []), 'status': 'FAILED'}
                    started = time.monotonic()
                    try:
                        answer, tool_trace, messages, observed_run, summary = await asyncio.wait_for(llm._run_agent(
                            row['rawUserText'], history=prior, task_state=state, on_task_state=capture,
                            domain_hint='ecommerce', session_id=lane.session_id, evaluation_context_arm=capability), timeout=180)
                        matched = observed_run == lane.run_id
                        stop_like = any(s in answer for s in ('无法继续执行', '状态构建失败', '安全停止', '超过了总执行时间限制'))
                        okay = matched and summary is not None and summary.control_policy == 'react_v1' and summary.agent_status == 'ok' and not stop_like
                        record.update(finalAnswer=answer, observedRunId=observed_run, runIdentityMatched=matched,
                            safeStopLikeAnswer=stop_like, status='SUCCEEDED' if okay else 'FAILED',
                            summary=summary.model_dump(by_alias=True, mode='json') if summary is not None else None)
                    except Exception as exc:
                        record.update(finalAnswer='', errorType=type(exc).__name__, errorMessage=str(exc)[:1500])
                        final_state = await runtime.load_task_state(row, lane)
                    record['durationMs'] = (time.monotonic()-started)*1000
                    calls = capability.ledger.snapshot()
                    record.update(modelCalls=calls['modelCalls'], toolCalls=calls['toolCalls'],
                        postStateRevision=final_state.revision, contextBindingHash=capability.context_binding_hash)
                    dialogue[arm] += [{'role': 'user', 'content': row['rawUserText']}, {'role': 'assistant', 'content': record['finalAnswer']}]
                    record['dialogue'] = dialogue[arm][:]
                    record['dialogueSha256'] = sha(record['dialogue'])
                    source_turn = next(t for t in conversation['turns'] if t['turnId'] == row['turnId'])
                    if 'expectedHard' in source_turn:
                        from agent.evaluation.context_program_v2_20260904.datasets import check_requirements
                        record['oracleErrors'] = check_requirements(final_state.domain_state.get('shoppingGuide', {}), source_turn)
                        if record['oracleErrors']: record['status'] = 'SEMANTIC_FAILURE'
                    append(out / 'outputs.jsonl', record)
                    append(out / 'private_states.jsonl', {'executionOrdinal': row['executionOrdinal'],
                        'recordSha256': sha(record), 'before': state.model_dump(by_alias=True, mode='json'),
                        'after': final_state.model_dump(by_alias=True, mode='json')})
                    results.append(record)
                    lanes[arm] = LaneBinding(lane.run_id, lane.task_id, lane.session_id, lane.branch_point_state_hash, final_state.revision)
                    print(f'P4 {len(results)}/{len(schedule)} {cid} t{row["semanticTurn"]} {arm} {record["status"]} calls={len(calls["modelCalls"])}', flush=True)
                    if client.halted: raise RuntimeError('provider_budget_or_transport_halted')
        except Exception as exc:
            json_new(out / 'stop.json', {'at': now(), 'errorType': type(exc).__name__, 'error': str(exc)[:300], 'completed': len(results)})
    summary = {arm: dict(Counter(r['status'] for r in results if r['arm'] == arm)) for arm in ARMS}
    result = {'status': 'PASS_EXECUTION' if len(results) == len(schedule) and all(r['status']=='SUCCEEDED' for r in results) else 'HOLD_RUNTIME_FAILURE',
        'completedArmTurns': len(results), 'plannedArmTurns': len(schedule), 'summary': summary,
        'runIdentityMismatches': sum(r.get('runIdentityMatched') is False for r in results),
        'safeStops': sum(bool(r.get('safeStopLikeAnswer')) for r in results),
        'modelCalls': sum(len(r['modelCalls']) for r in results),
        'qualityBoundary': 'execution and structured requirement checks, not human answer-quality labels',
        'productionDefaultsChanged': False}
    check_freeze(out / 'source_freeze.json')
    json_new(out / 'result.json', result)
    print(json.dumps(result), flush=True)


def main(args):
    p2 = json.loads((HERE / 'p2/attempt002/result.json').read_text(encoding='utf-8'))
    if p2['status'] != 'PASS': raise RuntimeError('P2_gate_not_passed')
    out = HERE / 'p4' / args.attempt
    out.mkdir(parents=True, exist_ok=False)
    data = rows(args.dataset)
    json_new(out / 'conversations.json', data)
    json_new(out / 'protocol.json', {'at': now(), 'pid': os.getpid(), 'dataset': str(args.dataset),
        'datasetSha256': file_sha(args.dataset), 'conversations': len(data),
        'userTurns': sum(len(c['turns']) for c in data), 'strict': p2['selectedStrict'],
        'maxTokens': 4096, 'sdkRetries': 0, 'turnDeadlineSeconds': 180,
        'multiAgent': False, 'memory': False, 'catalogSha256': '725c5fe9209c0b278004c61d24dafab21593c128e679ea0a1ecf3ae4eb433d75',
        'noDefaultSwitch': True, 'oldFailuresRetained': True})
    freeze(out / 'source_freeze.json')
    with owned_redis(out): asyncio.run(run(out, data, p2['selectedStrict']))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--attempt', required=True)
    parser.add_argument('--dataset', type=Path, required=True)
    args = parser.parse_args()
    if not __import__('re').fullmatch('[a-z]+[0-9]{3}', args.attempt): raise ValueError('invalid_attempt')
    main(args)
