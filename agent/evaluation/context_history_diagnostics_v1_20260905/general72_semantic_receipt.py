"""Bound factual checks supporting the manually read general72 semantic audit.

No grading, score replacement, model invocation or SUT changes.
"""
import json
from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, file_sha, sha, now, write_new
from agent.evaluation.context_history_review_v2_20260905.evidence import validated_turn_evidence
from .static_grid import captured_archive


def run():
    cohort = HERE / 'core72_v7_general_cohort001'
    closeout = HERE / 'core72_v7_general_review_resume_closeout001.json'
    if file_sha(closeout) != '2a9c2e615559cb21adf732260b7fb1f4a05e7c9a751a0f5c391c3e9d91a0a6f7':
        raise ValueError('closeout_drift')
    bindings, checks = {}, {}
    def turn(arm, n):
        path = cohort / (arm + '001') / ('turn-%02d.json' % n)
        bindings[str(path.relative_to(HERE))] = file_sha(path)
        return json.loads(path.read_text(encoding='utf-8'))
    final = {}
    for arm in 'ABC':
        rows = {n: turn(arm, n) for n in (19, 21, 26, 36, 37, 49, 62, 68, 70, 71, 72)}
        repeated = []
        for earlier, later in ((36, 37), (62, 68)):
            row = rows[later]
            assert row['answer'] == rows[earlier]['answer']
            assert not row['toolTraces']
            assert not validated_turn_evidence(row)['validationPassedThisTurn']
            assert validated_turn_evidence(rows[earlier])['validationPassedThisTurn']
            repeated.append({'earlier': earlier, 'later': later, 'answerSha256': sha(row['answer']),
                'nativeCalls': len(row['modelCalls']), 'currentTools': 0,
                'interpretation': 'OLD_VALIDATED_FACTS_REUSED_BUT_CURRENT_USER_TASK_UNANSWERED'})
        evidence = validated_turn_evidence(rows[70])
        final[arm] = {'turn70Evidence': evidence, 'turn72Answer': rows[72]['answer'],
            'turn72NativeCalls': len(rows[72]['modelCalls']),
            'budget19Query': rows[19]['query']}
        checks[arm] = {'cachedWrongTask': repeated,
            'turn21Evidence': validated_turn_evidence(rows[21]),
            'turn26Answer': rows[26]['answer']}
    assert len({checks[a]['cachedWrongTask'][1]['answerSha256'] for a in 'ABC'}) == 1
    records, archive_sha = captured_archive(cohort / 'B001/archive')
    source = next(r for r in records if r['messageId'] == 'msg-ab5b5b8299403ff64e2b89ad')
    assert source['role'] == 'user' and source['turn'] == 37
    assert source['content'] == turn('B', 37)['query']
    assert source['messageId'] in turn('B', 49)['answer']
    write_new(HERE / 'core72_v7_general_semantic_receipt001.json', {
        'at': now(), 'status': 'SCOPED_SEMANTIC_AUDIT_EVIDENCE_NOT_REGRADING',
        'closeoutSha256': file_sha(closeout), 'turnFileBindings': bindings,
        'falsePositiveB49': {'archiveSha256': archive_sha, 'actualSource': source,
            'disposition': 'REJECT_BOTH_FABRICATED_REFERENCE_CLAIMS_ID_EXISTS'},
        'checks': checks, 'unflaggedFinalAnswerSpotChecks': final,
        'scope': 'All primary <=2 cases and all serious claims manually read, plus final72 each arm. Not an exhaustive new independent grading.',
        'scoresChanged': False, 'modelCalls': 0, 'formalAcceptance': False,
        'remainingRisk': 'Common stale-result wrong-task/current-run provenance issue at37/68. B72 misses budget19 restoration and retains obsolete70 transport reserve.'})
    print(json.dumps({'status': 'BOUND', 'filesBound': len(bindings), 'modelCalls': 0}))


if __name__ == '__main__':
    run()
