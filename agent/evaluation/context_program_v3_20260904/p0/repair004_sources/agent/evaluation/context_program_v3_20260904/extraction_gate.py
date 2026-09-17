"""P2 repair regression: existing 24 development cases; expanded semantic oracle."""
import argparse
import asyncio
import json
from collections import Counter

from openai import AsyncOpenAI
from agent.app import llm
from agent.app.domains.ecommerce.shopping_state_authority import bind_authoritative_write
from agent.evaluation.context_program_v2_20260904.datasets import (
    extraction_cases, check_requirements, synthetic_state,
)
from .common import HERE, RecordedClient, BudgetStop, append, check_freeze, freeze, file_sha, json_new, now


def oracle(state, payload, bound, case):
    guide = bound['shoppingGuide']
    errors = check_requirements(guide, case)
    goal = payload.get('goal', state.goal)
    if not isinstance(goal, str) or goal.strip().casefold() in {'', 'null', 'none', 'undefined'}:
        errors.append('invalid_goal')
    if payload.get('status', state.status) != 'ready' or payload.get('pendingQuestions') or payload.get('addUnknowns'):
        errors.append('wrong_status_or_blocking_question')
    if guide['mode'] != 'recommend' or guide['category'] != 'phone': errors.append('mode_category')
    if case['familyId'] == 'preserve_hard':
        # Literal user clause is independent of the SUT's aspect label/parser.
        clause = case['message'].split('，')[-1].rstrip('。')
        if not any(u.endswith(':' + clause) for u in guide['useCases']): errors.append('qualitative_preference_lost')
        if any(r['key'] in {'battery_hours', 'battery_mah'} for r in guide['requirements']): errors.append('invented_battery_threshold')
    if bound['shoppingTaskStateV2']['shoppingGuide'] != guide: errors.append('authority_projection')
    return errors


async def run(attempt):
    out = HERE / 'p2' / attempt
    out.mkdir(parents=True, exist_ok=False)
    cases = extraction_cases()
    json_new(out / 'cases_input.json', cases)
    json_new(out / 'protocol.json', {'at': now(), 'baseCalls': 96, 'maxCalls': 192,
        'blocks': 2, 'modes': [False, True], 'caseSha256': file_sha(out / 'cases_input.json'),
        'kind': 'REPAIR_DEVELOPMENT_REGRESSION_NOT_HUMAN_HOLDOUT',
        'maxStructuredRepair': 1, 'passCriterion': '48/48 in mode; strict preferred if both pass',
        'oracle': ['hard_requirements', 'goal_placeholder', 'ready_status', 'recommend_phone',
                   'literal_qualitative_preference_retention', 'no_battery_threshold_invention', 'authority_projection'],
        'stateWrites': 0, 'businessToolCalls': 0, 'productionDefaultsChanged': False})
    freeze(out / 'source_freeze.json')
    # Store the actual runner source, not just a pointer to a mutable worktree.
    import shutil
    shutil.copyfile(__file__, out / 'runner_source.py.txt')
    results = []
    llm.settings.deepseek_base_url = 'https://api.deepseek.com/beta'
    llm.settings.task_state_extraction_max_tokens = 4096
    try:
        async with AsyncOpenAI(api_key=llm.settings.deepseek_api_key, base_url=llm.settings.deepseek_base_url,
                               timeout=45, max_retries=0) as provider:
            client = RecordedClient(provider, phase='P2', output=out)
            for block in (1, 2):
                for index, case in enumerate(cases):
                    modes = (False, True) if (index + block) % 2 else (True, False)
                    for strict in modes:
                        llm.settings.task_state_extraction_strict_enabled = strict
                        client.binding = {'attempt': attempt, 'caseId': case['caseId'], 'block': block, 'strict': strict}
                        state = synthetic_state(case['caseId'])
                        messages = [{'role': 'system', 'content': llm.TASK_STATE_PLANNING_PROMPT},
                            {'role': 'system', 'content': '当前已持久化状态：' + state.model_dump_json(by_alias=True)},
                            {'role': 'user', 'content': case['message']}]
                        result = {**client.binding, 'familyId': case['familyId'], 'status': 'FAILED', 'repairUsed': False}
                        try:
                            call, response = await llm._submit_task_state_extraction(client, messages)
                            try:
                                args = llm._parse_task_state_arguments(call, response=response, strict=strict)
                                payload, _ = llm._build_validated_task_state_payload(state, args, message=case['message'], require_status=True)
                            except llm.TaskStatePayloadValidationError as exc:
                                result['initialValidationError'] = {'code': exc.code, 'message': str(exc)}
                                if call is None: raise
                                result['repairUsed'] = True
                                args, _ = await llm._repair_task_state_payload(client, planning_messages=messages,
                                    original_call=call, validation_error=exc)
                                payload, _ = llm._build_validated_task_state_payload(state, args, message=case['message'], require_status=True)
                            domain = {**state.domain_state, **payload.get('domainStatePatch', {})}
                            bound = bind_authoritative_write({k: v for k, v in domain.items() if v is not None},
                                task_id=state.task_id, task_revision=state.revision+1, goal=payload.get('goal', state.goal),
                                unknowns=payload.get('addUnknowns', state.unknowns), pending_questions=payload.get('pendingQuestions', state.pending_questions))
                            result['oracleErrors'] = oracle(state, payload, bound, case)
                            result['effectiveGuide'], result['effectiveGoal'] = bound['shoppingGuide'], payload.get('goal', state.goal)
                            result['status'] = 'PASS' if not result['oracleErrors'] else 'SEMANTIC_FAILURE'
                        except BudgetStop: raise
                        except Exception as exc:
                            result.update(errorType=type(exc).__name__, errorCode=getattr(exc, 'code', None), errorMessage=str(exc)[:1500])
                        results.append(result)
                        append(out / 'cases.jsonl', result)
                        print(f"P2 {len(results)}/96 strict={strict} {case['caseId']} {result['status']}", flush=True)
                        if client.halted: raise BudgetStop('three_consecutive_transport_errors')
    except Exception as exc:
        json_new(out / 'stop.json', {'at': now(), 'errorType': type(exc).__name__, 'code': str(exc)[:180], 'completed': len(results)})
    check_freeze(out / 'source_freeze.json')
    summary = {str(mode): dict(Counter(r['status'] for r in results if r['strict'] == mode)) for mode in (False, True)}
    eligible = [mode for mode in (True, False) if summary[str(mode)] == {'PASS': 48}]
    result = {'status': 'PASS' if eligible else 'HOLD_EXTRACTION_SEMANTICS', 'completed': len(results),
              'summary': summary, 'selectedStrict': eligible[0] if eligible else None,
              'stateWrites': 0, 'businessToolCalls': 0, 'productionDefaultsChanged': False}
    json_new(out / 'result.json', result)
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--attempt', required=True)
    args = parser.parse_args()
    if not __import__('re').fullmatch(r'attempt\d{3}', args.attempt): raise ValueError('invalid_attempt')
    asyncio.run(run(args.attempt))
