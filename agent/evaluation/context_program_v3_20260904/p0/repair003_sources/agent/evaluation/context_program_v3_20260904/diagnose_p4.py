"""Offline source-state replay, not new live response evidence."""
from types import SimpleNamespace
from .common import HERE, rows, json_new, now, sha
from agent.app.task_state import TaskState
from agent.app.control.react_context import build_decision_context_view
from agent.app.control.react_decision import deterministic_next_action
from agent.app.validator import _validate_guide_decision, settings
from agent.evaluation.real_user_multiturn_ab_executor_20260902_v2 import lane_runtime as old

out = HERE / 'p4/repair002_diagnostic_v2.json'
states = rows(HERE / 'p4/regression001/private_states.jsonl')
observations = []
for r in states:
    if r['executionOrdinal'] not in [5,13,14,41,42,51,52,57,58]: continue
    state = TaskState.model_validate(r['after'])
    v = build_decision_context_view(state, user_message='综合权衡',
        allowed_tool_names=['search_products','compare_products'])
    observations.append({'row':r['executionOrdinal'], 'sourceStateSha256':sha(r['after']),
        'trigger':v.observation_summary.adaptive_trigger, 'options':[x.option_id for x in v.allowed_action_options],
        'validationOutcome':v.observation_summary.validation_outcome,
        'deterministic':bool(deterministic_next_action(v))})
products = tuple(old._product_from_catalog(r) for r in old._read_jsonl(old.CATALOG_PATH))
transport = old.FrozenCatalogTransport(products)
req = {'key':'price_minor','operator':'lte','value':220000,'unit':'CNY_MINOR','priority':'hard','source':'user'}
args = {'productIds':[956972], 'category':'phone','requirements':[
    old.ShoppingRequirement.model_validate(req).model_dump()]}
trace = transport._compare(args)
context = SimpleNamespace(step=SimpleNamespace(tool_name='compare_products'),
    execution_result=SimpleNamespace(tool_trace=trace, resolved_arguments=args))
before = _validate_guide_decision(context)
settings.used_phone_synthetic_price_dir = str(old.CATALOG_DIR)
settings.used_phone_synthetic_price_policy = 'budget_and_ranking'
after = _validate_guide_decision(context)
result = {'at':now(),'modelCalls':0,'stateReplays':observations,
    'syntheticPriceOriginalValidation':before, 'syntheticPriceAlignedValidation':after,
    'passed': all(r['trigger']=='bounded_action_choice' or r['deterministic']
       or r['validationOutcome']!='passed' for r in observations)
       and before[1]=='synthetic_price_evidence_mismatch' and after[0]=='satisfied',
    'boundary':'deterministic diagnostic; no new live outcomes; unchanged Validator'}
json_new(out,result)
print({k:v for k,v in result.items() if k!='stateReplays'})
