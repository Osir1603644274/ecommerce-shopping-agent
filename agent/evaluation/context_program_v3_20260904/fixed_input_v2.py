"""Repair the fidelity instruction's ambiguous source-to-output field mapping."""
import asyncio
from copy import deepcopy
import json
from .common import HERE, json_new, now
from . import fixed_input


PATHS = {
    'currentGoal': 'context.goal',
    'candidateIds': 'context.candidateScopeState.rankedItemIds',
    'evidenceRefs': 'context.evidenceRefs',
    'recentReference': 'context.referenceContextState.recentReference',
    'referenceSource': 'context.referenceContextState.sourceKind',
    'referenceTaskRevision': 'context.referenceContextState.referenceTaskRevision',
    'referenceScopeSourceRevision': 'context.referenceContextState.candidateScopeSourceRevision',
    'focusedProductId': 'context.referenceContextState.focusedProductId',
    'comparedProductIds': 'context.referenceContextState.comparedProductIds',
}


async def main():
    gate = json.loads((HERE / 'p2/attempt003/result.json').read_text(encoding='utf-8'))
    if gate['status'] != 'PASS': raise RuntimeError('current_repair_P2_not_passed')
    json_new(HERE / 'p3/instruction_repair002.json', {'at': now(),
        'reason': 'referenceTaskRevision null was twice confused with contextTaskRevision=1',
        'change': 'explicit per-field JSON source paths; no oracle-derived answer or sample deletion',
        'paths': PATHS, 'oldAttemptRetained': True, 'sameFortyCases': True,
        'budgetTransfer': '240 from unstarted P5, no total cap increase'})
    fixed_input.v4.SYSTEM_PROMPT = (
        'Call record_context_fidelity_v4 exactly once. This is exact copying, not inference. '
        'For each output field, copy ONLY from its explicit JSON source path:\n'
        + json.dumps(PATHS, sort_keys=True) + '\n'
        'A source JSON null MUST remain JSON null, without quotes. '
        'referenceTaskRevision is NOT contextTaskRevision or baseContextRevision. '
        'Never fill a null from a similarly named field. sourceKind NONE still has null reference fields. '
        'Do not translate, reorder, normalize, infer, or borrow values from historySummaries.'
    )
    tool = deepcopy(fixed_input.v4.FIDELITY_TOOL)
    for name, path in PATHS.items():
        tool['function']['parameters']['properties'][name]['description'] = 'Copy exactly from ' + path + '; preserve JSON null if present.'
    fixed_input.v4.FIDELITY_TOOL = tool
    await fixed_input.run('attempt002')


if __name__ == '__main__': asyncio.run(main())
