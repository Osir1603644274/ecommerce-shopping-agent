"""Family-level check and explicit coverage of all 11 fallacy categories."""
from collections import defaultdict
import json
from .common import HERE, V3, rows, json_new, now, file_sha


def main():
    data = rows(V3 / 'p1/breadth001/confirm.jsonl')
    family = {c['conversationId']: c['primitiveFamily'] for c in data}
    values = defaultdict(lambda: defaultdict(int))
    for root, attempt in ((V3, 'confirm001'), (HERE, 'recovery001')):
        ledger = rows(root / 'provider_ledger.jsonl')
        ends = {e['requestId']: e for e in ledger if e['event'] == 'END'}
        for event in ledger:
            if event['event'] != 'START' or event['binding'].get('attempt') != attempt:
                continue
            b = event['binding']
            if root == V3 and int(b['conversationId'].rsplit('-',1)[1]) >= 55:
                continue
            cost = (ends[event['requestId']].get('usage') or {}).get('total_tokens', event['reservedTokens'])
            values[family[b['conversationId']]][b['arm']] += cost
    families = [{'family': f, 'controlTokens': v['RAW_FULL_CONTROL'], 'treatmentTokens': v['CONTEXT_TREATMENT'],
                 'ratio': v['CONTEXT_TREATMENT'] / v['RAW_FULL_CONTROL']} for f,v in sorted(values.items())]
    analysis = json.loads((HERE / 'p8/analysis.json').read_text(encoding='utf-8'))
    total_ratio = sum(r['treatmentTokens'] for r in families) / sum(r['controlTokens'] for r in families)
    assert total_ratio == analysis['coherentTokenRatio'] and len(families) == 8
    scan = [
        {'type': 'Simpson paradox', 'status': 'CHECKED', 'note': 'Aggregate and every family cost ratio are recorded; do not claim all families improve if any ratio exceeds one.'},
        {'type': 'Ecological fallacy', 'status': 'CAUTION', 'note': 'Family aggregates and synthetic turn successes do not establish real-user individual success rates.'},
        {'type': 'Berkson selection', 'status': 'CAUTION', 'note': 'Same-agent synthetic corpus selected for Context stress is not representative user sampling.'},
        {'type': 'Collider bias', 'status': 'CHECKED_NOT_MODELED', 'note': 'No outcome-conditioned regression adjustment; complete protocol-defined conversation versions retained.'},
        {'type': 'Base rate neglect', 'status': 'CAUTION', 'note': 'Actual error prevalence in real traffic is unknown; exact observed denominators are reported.'},
        {'type': 'Regression to mean', 'status': 'CAUTION', 'note': 'Three refusal failures were selected for operational recovery; improvement cannot establish a causal model-quality gain.'},
        {'type': 'Survivorship bias', 'status': 'CHECKED', 'note': 'All 1221 physical runs, 1200 first-attempt outcomes, and 1200 coherent-version outcomes are separate; original failures and all costs retained.'},
        {'type': 'Look elsewhere', 'status': 'CHECKED', 'note': 'All historical attempts retained; no p-values or selected significant superiority claim.'},
        {'type': 'Forking paths', 'status': 'CAUTION', 'note': 'Recovery protocol specified before v4 outcomes; prior confirmation exposure and code tuning remain disclosed. Not new blind confirmation.'},
        {'type': 'Correlation causation', 'status': 'CAUTION', 'note': 'Versioned descriptive comparisons and differing action/call paths do not establish general causal superiority.'},
        {'type': 'Reverse causality', 'status': 'CHECKED_NOT_APPLICABLE', 'note': 'Assigned context arms precede the observed outputs; no observational reverse-causality claim is made.'},
    ]
    result = {'at': now(), 'verificationStatus': 'ANALYZED', 'overallConfidence': 'CAUTION',
        'fallacyCoverage': '11/11', 'fallacyScan': scan, 'familyCosts': families,
        'familiesWithTokenRatioAboveOne': [r['family'] for r in families if r['ratio'] > 1],
        'familyAggregateRecomputed': True, 'analysisSha256': file_sha(HERE / 'p8/analysis.json'),
        'reproducibility': 'N/A external API exact reproduction; local source and receipt hashes audited',
        'noPopulationNIAcceptance': True, 'noHumanQualityLabels': True}
    json_new(HERE / 'p8/statistical_validation.json', result)
    print(json.dumps({'status': 'ANALYZED', 'fallacyCoverage': '11/11', 'families': families}, indent=2))


if __name__ == '__main__':
    main()
