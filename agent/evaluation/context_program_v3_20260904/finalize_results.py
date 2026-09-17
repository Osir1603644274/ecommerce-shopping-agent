"""Build machine-readable bounded decisions only after all selected runs end."""
from collections import Counter
import json
from xml.etree import ElementTree as ET
from .common import HERE, ROOT, check_freeze, file_sha, json_new, now
from .audit_ledger import audit

def read(path):return json.loads((HERE/path).read_text(encoding='utf-8'))
def main():
    attempts={name:read(f'p4/{name}/result.json') for name in ('regression004','dev003','confirm001')}
    for name,record in attempts.items():
        if name != 'confirm001':
            assert record['completedArmTurns']==record['plannedArmTurns'],'main_attempt_incomplete'
        else:
            assert record['status']=='HOLD_RUNTIME_FAILURE'
            assert record['completedArmTurns'] < record['plannedArmTurns']
            assert read('p4/confirm001/provider_balance_diagnosis.json')['status']=='BLOCKED_PROVIDER_BALANCE'
        assert read(f'p4/{name}/integrity_v2.json')['status']=='PASS_RUNNER_EVIDENCE_EDGES'
        # New independent evidence scripts may have been added, but every
        # application file used by each selected live run must still match.
        for row in read(f'p4/{name}/source_freeze.json')['sources']:
            if row['path'].startswith('agent/app/'):
                assert file_sha(ROOT/row['path'])==row['sha256'],'selected_app_source_drift'
    analysis=read('p4/confirm001/analysis.json');fixed=read('p3/attempt002/analysis.json')
    ledger=audit();assert ledger['status']=='PASS_LEDGER_EDGES'
    assert ledger['unresolvedRequestIds']==['ctxv3-call-00036'],'new_unresolved_requests_need_audit'
    history=read('p0/historical_verification.json');assert history['passed']
    full=read('p8/full005/result.json');source=read('p8/full005/source_verification.json')
    guard=read('p8/full005/http_guard.json');assert source['passed'] and guard['externalProviderCalls']==0
    suites=list(ET.parse(HERE/'p8/full005/pytest.xml').getroot().iter('testsuite'))
    tests={key:sum(int(s.get(key,'0')) for s in suites) for key in ('tests','failures','errors','skipped')}
    tests['passed']=tests['tests']-tests['failures']-tests['errors']-tests['skipped']
    tests['sourceFiles']=source['sourceFiles'];tests['externalHTTPAttemptsBlocked']=len(guard['blockedRequests'])
    tests['externalProviderCalls']=0
    browser=read('p6/browser001/verification.json');recovery=read('p6/process002/verification_v2.json')
    race=read('p6/revisionrace001/observations.json');interrupt=read('p6/interrupt001/verification.json')
    contracts=read('p7/offline_contracts.json')
    assert browser['status']=='PASS_BOUNDED_BROWSER_ENTRY' and recovery['status']=='PASS_BOUNDED_RECOVERY_MATRIX'
    assert race['passed'] and interrupt['status']=='PASS_BOUNDED_PROCESS_RECOVERY'
    assert contracts['status']=='PASS_OFFLINE_CONTRACTS'
    main_execution=all(r['status']=='PASS_EXECUTION' for r in attempts.values())
    full_pass=full['exitCode']==0 and tests['failures']==tests['errors']==0
    ni=analysis['formalPopulationNI']=='PASS'
    dependent_gate=main_execution and analysis['costThresholdMet'] and ni
    if dependent_gate:
        raise RuntimeError('dependent_online_extensions_are_now_eligible_and_must_finish_before_finalization')
    reasons=['PROVIDER_402_INSUFFICIENT_BALANCE_REQUIRES_ACCOUNT_ACTION',
        'CONFIRMATION_INCOMPLETE_165_ARM_TURNS_NOT_EXECUTED']
    if not main_execution:reasons.append('THREE_PROVIDER_REFUSALS_CAUSED_SAFE_STOPS_OR_UNMET_REQUEST')
    if not analysis['costThresholdMet']:reasons.append('CONFIRMATION_EFFICIENCY_GATE_NOT_EVALUABLE')
    if not ni:reasons.append('POPULATION_NI_NOT_ESTABLISHED_FROM_EIGHT_SYNTHETIC_FAMILIES')
    reasons.append('HUMAN_ANSWER_QUALITY_NOT_MEASURED')
    if not fixed['tokenGate']:reasons.append('FIXED_INPUT_10_PERCENT_TOKEN_TARGET_NOT_MET')
    if not full_pass:reasons.append('FINAL_OFFLINE_SUITE_FAILED')
    if not dependent_gate:
        withheld={'at':now(),'status':'NOT_RUN_DEPENDENT_MAIN_GATE_AND_PROVIDER_402','reasons':reasons,
            'mainResultSha256':file_sha(HERE/'p4/confirm001/result.json'),
            'mainAnalysisSha256':file_sha(HERE/'p4/confirm001/analysis.json'),
            'newRequests':0,'notABudgetExhaustion':ledger['remainingRequests']>0 and ledger['remainingTokens']>0,
            'noRelaxedGateNoExtraConfirmationSamples':True}
        json_new(HERE/'p5/online001/NOT_RUN.json',withheld)
        json_new(HERE/'p7/online_combinations_NOT_RUN.json',withheld)
    json_new(HERE/'p8/ledger_final.json',ledger)
    phase_status={
        'P0':'PASS_HISTORICAL_AND_SOURCE_ARCHIVE_INTEGRITY',
        'P1':'FROZEN_SYNTHETIC_16_DEV_64_CONFIRM_EIGHT_FAMILIES',
        'P2':'PASS_BOUNDED_EXTRACTION_AND_REPAIR_PROBES',
        'P3':'PASS_FIXED_INPUT_FIDELITY_COST_HOLD' if not fixed['tokenGate'] else 'PASS_BOUNDED_FIXED_INPUT',
        'P4':'BLOCKED_PROVIDER_BALANCE_PARTIAL_CONFIRMATION',
        'P5':'PASS_OFFLINE_ONLINE_NOT_RUN' if not dependent_gate else 'READY_FOR_ONLINE',
        'P6':'PASS_BOUNDED_RECOVERY_AND_BROWSER_ENTRY',
        'P7':'PASS_OFFLINE_CONTRACTS_ONLINE_NOT_RUN' if not dependent_gate else 'READY_FOR_ONLINE',
        'P8':'PASS_FINAL_TESTS_EXPERIMENT_ACCEPTANCE_HOLD' if full_pass else 'HOLD_TESTS',
    }
    decision={'at':now(),'programId':HERE.name,'status':'BLOCKED_PROVIDER_BALANCE_PARTIAL_EXECUTION',
        'allP0P8Passed':False,'allOnlineExtensionsExecuted':False,'productionDefaultSwitchAllowed':False,
        'productionDefaultsChanged':False,'scope':'current bounded registered program, not general superiority or production readiness',
        'phaseStatus':phase_status,'holdReasons':reasons,'selectedP4Attempts':attempts,
        'fixedInput':fixed,'confirmation':analysis,'fullSuite':tests,'budget':ledger,
        'browser':{'httpRequests':browser['httpRequests'],'providerCalls':browser['providerCalls'],'status':browser['status']},
        'recovery':{'matrixCases':recovery['caseCount'],'matrixPassedCases':recovery['passedCases'],
            'revisionRacePassed':race['passed'],'interruptChecks':len(interrupt['checks']),
            'externalTransactionExactlyOnce':'NOT_PROVEN'},
        'offlineCombinationContracts':{'cases':contracts['cases'],'groups':contracts['groups']},
        'humanBoundary':['Restore provider account balance or configure an authorized funded account locally; do not expose API keys in chat. No further paid calls before user confirms readiness.',
            'Independent real-user or independently authored task sampling is needed for population claims.',
            'Answer-quality judgments, including noted comparison-prose overreach, remain unjudged.'],
        'reproducibility':'API nondeterminism: no exact live-response reproduction claim; offline rescoring retains raw outputs.',
        'historicalFailuresRetained':True,'confirmationExpandedAfterOutcomes':False,
        'authoringBoundary':'same-agent synthetic data and automated oracles; no human gold/qrel labels were generated'}
    json_new(HERE/'FINAL_DECISION.json',decision)
    print({'status':decision['status'],'phases':phase_status,'tests':tests,'requests':ledger['totalRequests'],
        'chargedTokens':ledger['chargedTokens'],'confirmTokenRatio':analysis['totalTokenRatio'],'holdReasons':reasons})

if __name__=='__main__':main()
