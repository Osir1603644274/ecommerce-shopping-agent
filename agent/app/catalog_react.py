"""Catalog adapter for the existing bounded ReAct decision contract.

Reuses the production model decider + strict option materialization. Document
scopes are NOT phone CandidateScopes or commerce authorization. Journal storage
remains owner-bound workspace checkpoints, not GraphV2 ToolInbox receipts.
"""
import asyncio
import time
from .catalog_service import fingerprint, verify_scope
from .settings import settings
from .control.react_context import DecisionContextView, DecisionActionOption, DecisionToolOption
from .control.react_decision import decide_next_action, materialize_next_action, ReactActionProposal


def decision_view(run):
    if run['catalogPlan']['route']=='product':
        return product_decision_view(run), {}
    current = run['catalogNext']
    scope = current.get('scope')
    verify_scope(scope)
    tried = run.get('catalogQueries', [])
    query = current.get('retrievalQuery') or current.get('query', '')
    # Alternate wording uses only the current server-validated positive terms.
    # Never drop/modify requirements to make a result appear to match.
    alternate = ' '.join(dict.fromkeys(r['value'] for r in current.get('requirements', [])
        if r['mode'] == 'require' and r['facet'] != '预算'))
    choices = list(dict.fromkeys(q for q in (query, alternate) if q.strip() and q not in tried))
    searching = run['catalogPlan']['action'] in {'search', 'refine', 'new'}
    options, tools, queries = [], [], {}
    if searching and len(tried) < 2:
        for index, candidate in enumerate(choices[:1]):
            name = f'catalog_search_{len(tried)+index+1}'
            refs = {'query': name, 'requirements': 'catalogNext.requirements'}
            options.append(DecisionActionOption(optionId='tool.'+name, kind='CALL_TOOL',
                reasonCode='retrieve_current_requirements', toolName='catalog_search', argumentRefs=refs))
            queries[name] = candidate
        if options:
            tools = [DecisionToolOption(name='catalog_search', argumentRefs=options[0].argument_refs)]
    answer_ref = 'catalog-evidence:' + fingerprint(scope)
    if scope is not None or run.get('catalogNotice') or not searching:
        options.append(DecisionActionOption(optionId='answer.current', kind='ANSWER',
            reasonCode='answer_available_evidence', answerContextRef=answer_ref))
    question = run['catalogPlan'].get('question') or '你更在意哪项条件？也可以说明希望调整的型号、用途或预算。'
    options.append(DecisionActionOption(optionId='clarify.requirements', kind='ASK_CLARIFICATION',
        reasonCode='clarify_current_requirements', question=question))
    if run['catalogPlan']['action'] == 'clarify':
        options = [options[-1]]
        tools, queries = [], {}
    elif run['catalogPlan']['action'] == 'cancel' or run.get('catalogNotice'):
        options = [o for o in options if o.kind == 'ANSWER']
        tools, queries = [], {}
    # After the model budget, terminate with evidence (including zero results)
    # or a clarification. Never manufacture another model decision.
    if run.get('catalogModelDecisions', 0) >= settings.agent_react_v1_max_model_decisions:
        options = [next((o for o in options if o.kind == 'ANSWER'), options[-1])]
        tools, queries = [], {}
    groups = (scope or {}).get('groups', [])
    view = DecisionContextView(taskId=run['id'], taskRevision=run.get('catalogDecisionRevision', 1),
        taskStatus='active', goal=current.get('query', '') or run['message'], userMessage=run['message'],
        shoppingMode='catalog_read_only', requirements=current.get('requirements', []),
        serverSignals={'retrieved':scope is not None, 'noCandidates':scope is not None and not groups},
        allowedActions=list(dict.fromkeys(o.kind for o in options)), allowedTools=tools,
        allowedActionOptions=options, answerContextRef=answer_ref if scope is not None else None,
        observationSummary={'candidatePoolCount':len(groups), 'rankedItemCount':len(groups),
            'validationOutcome':'scope_integrity_verified' if scope is not None else None},
        lastOutcome={'scopeId':(scope or {}).get('scopeId'), 'titles':[g['title'][:120] for g in groups[:6]],
            'queriesTried':tried, 'availableQueryBindings':queries,
            'evidenceBoundary':'Source titles only; no measurements or authority to pay.'},
        decisionViewHash='0'*64)
    body = view.model_dump(by_alias=True, mode='json'); body.pop('decisionViewHash')
    return view.model_copy(update={'decision_view_hash':fingerprint(body)}), queries


def product_decision_view(run):
    plan=run['catalogPlan'];anchor=plan.get('productContext')
    ambiguous=plan['action']=='clarify' or not anchor or plan.get('followup')=='ambiguous'
    question=plan.get('question') or ('你问的是商品随附的配件，还是想另外购买适配配件？'
        if plan.get('followup')=='ambiguous' else '请说明你指的是哪件商品，以及想了解的具体信息。')
    ask=DecisionActionOption(optionId='clarify.product',kind='ASK_CLARIFICATION',reasonCode='clarify_product_reference',question=question)
    options=[ask];tools=[];answer_ref=None
    if not ambiguous and 'productFacts' not in run:
        refs={'product':'catalogPlan.productContext'}
        tools=[DecisionToolOption(name='read_product_facts',argumentRefs=refs)]
        options=[DecisionActionOption(optionId='tool.read_product_facts',kind='CALL_TOOL',reasonCode='read_bound_product',toolName='read_product_facts',argumentRefs=refs)]
    elif not ambiguous:
        if fingerprint(run['productFacts'])!=run['catalogEvidenceSha256']:
            raise ValueError('product_question_evidence_changed')
        answer_ref='product-facts:'+run['catalogEvidenceSha256']
        options=[DecisionActionOption(optionId='answer.product',kind='ANSWER',reasonCode='answer_bound_product',answerContextRef=answer_ref),ask]
    if run.get('catalogModelDecisions',0)>=settings.agent_react_v1_max_model_decisions:
        options=options[:1]
    view=DecisionContextView(taskId=run['id'],taskRevision=run.get('catalogDecisionRevision',1),taskStatus='active',
        goal=run['message'],userMessage=run['message'],shoppingMode='product_read_only',
        serverSignals={'boundProduct':bool(anchor),'ambiguous':ambiguous},
        allowedActions=list(dict.fromkeys(o.kind for o in options)),allowedTools=tools,allowedActionOptions=options,
        answerContextRef=answer_ref,observationSummary={},
        lastOutcome={'product':{k:v for k,v in (anchor or {}).items() if k in {'number','title'}},
            'facts':{k:v[:240] for k,v in run.get('productFacts',{}).items()},'anchorHash':fingerprint(anchor)},
        decisionViewHash='0'*64)
    body=view.model_dump(by_alias=True,mode='json');body.pop('decisionViewHash')
    return view.model_copy(update={'decision_view_hash':fingerprint(body)})


async def decide(run):
    view, queries = decision_view(run)
    began = time.perf_counter()
    receipt = {'inputSha256':view.decision_view_hash, 'modelCalled':False}
    if len(view.allowed_action_options) == 1:
        action = materialize_next_action(ReactActionProposal(taskId=view.task_id,
            basedOnRevision=view.task_revision, decisionViewHash=view.decision_view_hash,
            optionId=view.allowed_action_options[0].option_id), view)
        source = 'server_boundary'
    else:
        from .catalog_model_client import borrow_client
        def received(response):
            receipt.update(responseId=response.id, model=settings.deepseek_model,
                usage=response.usage.model_dump() if response.usage else None)
        async with borrow_client() as client:
            action = await asyncio.wait_for(decide_next_action(view, client=client,
                model=settings.deepseek_model, on_response=received), settings.agent_react_decision_timeout_seconds)
        receipt['modelCalled'] = True
        source = 'model'
    option = next(o for o in view.allowed_action_options if o.kind == action.kind and
        o.reason_code == action.reason_code and o.argument_refs == action.argument_refs)
    return {'action':action.model_dump(by_alias=True,mode='json'), 'optionId':option.option_id,
        'viewHash':view.decision_view_hash, 'source':source, 'receipt':receipt,
        'query':queries.get((action.argument_refs or {}).get('query')),
        'durationMs':(time.perf_counter()-began)*1000}
