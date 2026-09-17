"""Write a bounded, source-bound semantic closeout after root review; no models."""
import json
from pathlib import Path
from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, now, write_new
from agent.evaluation.context_history_diagnostics_v1_20260905.close_interrupted_cohort import digest


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def main():
    cost_path = HERE / 'first_edition_rescue_cost001.json'
    quality_path = HERE / 'first_edition_ab_review_closeout001.json'
    output = HERE / 'first_edition_semantic_closeout001.json'
    if output.exists():
        raise ValueError('new_receipt_required')
    if digest(cost_path) != '097324967bcc3b388fb2f751f0524ead53e874aae84adf5c3f62feb2aca5b544':
        raise ValueError('cost_receipt_changed')
    cost, quality = read(cost_path), read(quality_path)
    review = HERE / 'first_edition_ab_review001'
    if read(review.with_name(review.name + '_supervisor') / 'result.json')['childExitCode'] != 0:
        raise ValueError('closed_review_required')
    if quality['seriousClaims'] or quality['unknownJudgeUsage'] or any(x['disputed'] for x in quality['decisions']):
        raise ValueError('unexpected_claims_unknowns_or_disputes_need_actual_audit')
    root_read = {'A': [1, 2, 4, 5, 15, 24, 28, 33, 37, 41, 47, 48],
                 'B': [1, 4, 5, 11, 15, 24, 28, 33, 37, 41, 45, 47, 48]}
    mapping = {x['sampleId']: Path(x['directory']).name[0] for x in read(review / 'mapping_PRIVATE_NOT_IN_JUDGE_INPUT.json')}
    flags, score_keys, source_hashes = [], set(), {}
    for path in sorted(review.glob('*judgment-*.json')):
        judge = int(path.stem.rsplit('-', 1)[1])
        if judge not in (1, 2):
            raise ValueError('unexpected_third_judge')
        source_hashes[str(path)] = digest(path)
        for score in read(path)['scores']:
            arm = mapping[score['sampleId']]
            key = (arm, score['turn'], judge)
            if key in score_keys:
                raise ValueError('duplicate_score')
            score_keys.add(key)
            if min(score[dim] for dim in ('correctness', 'constraints', 'relevance', 'usefulness')) < 4:
                if score['turn'] not in root_read[arm]:
                    raise ValueError('unreviewed_nonperfect_score')
                flags.append({'arm': arm, 'judge': judge, **score})
    if score_keys != {(a, t, j) for a in 'AB' for t in range(1, 49) for j in (1, 2)}:
        raise ValueError('incomplete_exact_double_coverage')
    cohort = HERE / 'first_edition_vivo48_cohort001'
    for arm, turns in root_read.items():
        for turn in turns:
            path = cohort / f'{arm}001/turn-{turn:02d}.json'
            actual = digest(path)
            if actual != cost['artifactHashes'][str(path.resolve())]:
                raise ValueError('root_review_turn_changed')
            source_hashes[str(path)] = actual
    b48 = read(cohort / 'B001/turn-48.json')
    active = b48['postState']['domainState']['shoppingTaskStateV2']['requirements']
    if not any(x['key'] == 'price_minor' and x['value'] == 160000 and x['priority'] == 'hard' for x in active):
        raise ValueError('unexpected_budget_state')
    if any(x['key'] in ('battery_originality', 'scratch_level', 'motherboard_repair') for x in active):
        raise ValueError('record_only_was_promoted_need_reassessment')
    summary_calls = []
    for line in (cohort / 'C001/native_call_bindings.jsonl').read_text(encoding='utf-8').splitlines():
        binding = json.loads(line)
        if binding.get('purpose') == 'history_summary':
            n = binding['nativeOrdinal']
            path = cohort / f'C001/model_calls/call-{n:03d}/result.json'
            row = read(path)
            source_hashes[str(path)] = digest(path)
            summary_calls.append({'ordinal': n, 'tokens': row['usage']['input_tokens'] + row['usage']['output_tokens'], 'durationMs': row['durationMs']})
    for path in (cost_path, quality_path, review / 'result.json', review.with_name(review.name + '_supervisor') / 'result.json', Path(__file__)):
        source_hashes[str(path)] = digest(path)
    sut = sum(cost['arms'][a]['census']['knownNativeTokenSubtotal'] for a in 'ABC')
    value = {
        'kind': 'FIRST_EDITION_LIMITED_AB_SEMANTIC_CLOSEOUT_C_INSUFFICIENT', 'at': now(),
        'scope': 'Frozen-vivo48 A/B only; C43 interrupted, no comparative C ratio or new experiments.',
        'ABResourceThresholdsObservedMet': cost['comparisons']['B_vs_A']['tokenAtLeast10Percent'] and cost['comparisons']['B_vs_A']['timeIncreaseAtMost15Percent'],
        'ABMeanQualityThresholdsMet': quality['comparisons']['B_PACK_VIEW']['eachDimensionLossAtMostPoint5'],
        'mandatoryJudgeCalls': 32, 'exactScoredAnswersPerJudge': 96, 'seriousClaimsFromJudges': 0,
        'rootAdditionalConfirmedSeriousErrors': [], 'allNonperfectScoreCasesReadByRoot': True,
        'rootReadTurns': root_read, 'rootSampleScope': 'Behavior-selected samples, not independent human gold or exhaustive second reading of every answer.',
        'nonperfectScoresPreserved': flags,
        'semanticFindings': [
            {'case': 'A5/B5', 'verdict': 'CONFIRMED_OUTPUT_OMISSION', 'detail': 'Only generic unknown-field disclaimer, no explicit list of unknown hearing/scanning/font/capacity items; original scores retained.'},
            {'case': 'A4/B4', 'verdict': 'CONFIRMED_BRIEF_ACKNOWLEDGMENT_OMISSIONS_NOT_PROOF_OF_CONTEXT_LOSS', 'detail': 'Answers omit club timing and bag detail; both occur early and this alone does not show Pack discarded raw history.'},
            {'case': 'B11', 'verdict': 'CONFIRMED_MISSING_INLINE_OS_CITATION', 'detail': 'Android sentence lacks OS evidence reference; rubric treats this as small format omission, not fabricated product fact.'},
            {'case': 'B45', 'verdict': 'CONFIRMED_LOCAL_REMINDER_OMISSION', 'detail': 'Lunch reminder omitted in this summary, explicitly present again in B48; do not call permanently lost history.'},
            {'case': 'B48', 'verdict': 'CONFIRMED_WORDING_AND_GROUPING_FLAW_NOT_STATE_CORRUPTION', 'detail': 'Budget described as synthetic-price cap and record-only fields placed under soft preference heading; underlying active requirements still hard budget160000, softiOS, originalscreen/normalcase/allowedbattery hard, no record-only field promotion.'},
            {'case': 'A41/B41 and A48/B48', 'verdict': 'REFERENCES_MATCH_OWN_PRIOR_DISPLAY', 'detail': 'Historical turn28 second product and recent turn47 first/second independently matched to each arm own actual answer, not another arm products.'},
            {'case': 'A48/B48', 'verdict': 'BUDGET_CHAIN_RETAINED_IN_ANSWER', 'detail': 'Both preserve1600->1400->1500->1440->1600, revoked/restored conditions and conditional plans; not proof oldgeneral72 entirety repaired.'}],
        'CPartialSummaryCalls': summary_calls,
        'CPartialSummaryTokens': sum(x['tokens'] for x in summary_calls),
        'CPartialSummaryMs': sum(x['durationMs'] for x in summary_calls),
        'newSutIncludingCFailedAttemptToken': sut,
        'twoProbeTokens': 153913, 'judgeTokens': quality['completeJudgeTokenTotal'],
        'newNativeKnownTotalSutProbesJudges': sut + 153913 + quality['completeJudgeTokenTotal'],
        'notIncludedInNativeExperimentLedger': 'Root research/orchestration conversation account usage; not claimed zero or complete account cost.',
        'limitedDevelopmentDecision': 'AB_OBSERVED_THRESHOLDS_MET_WITH_MINOR_ERRORS',
        'completeABCDecision': 'INSUFFICIENT_EVIDENCE_SOURCE_DRIFT_C_INCOMPLETE',
        'formalAcceptance': False, 'productionAcceptance': False, 'sourceHashes': source_hashes}
    write_new(output, value)
    print(json.dumps({k: value[k] for k in ('limitedDevelopmentDecision', 'completeABCDecision', 'newNativeKnownTotalSutProbesJudges')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
