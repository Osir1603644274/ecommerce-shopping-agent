"""Refresh this new run's recoverable state from actual completion receipts."""
from __future__ import annotations
import json
import os
from datetime import datetime,timezone
from bootstrap import ROOT,DEV,REPO,sha

def get(path):
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.is_file() else None

def main():
    dispatched=[get(p) for p in sorted((DEV/'dispatch').glob('*.json'))]
    collected=[get(p) for p in sorted((DEV/'reviews').glob('*/COLLECTED.json'))]
    status={
      'updated_at':datetime.now(timezone.utc).isoformat(),'complete':False,
      'goal_status':'active','stage':'development_blind_review_and_implementation',
      'assets':get(ROOT/'assets/COMPLETE.json'),
      'development_policy':get(DEV/'policy/FROZEN.json'),
      'development_packets':len(get(DEV/'packets/MANIFEST.json')['packets']) if (DEV/'packets/MANIFEST.json').exists() else 0,
      'reviews_dispatched':len(dispatched),'reviews_collected':len(collected),
      'review_pairs_collected':sum(x['validation']['pairs'] for x in collected),
      'independent_review_tasks':[{'review_id':r['review_id'],'thread_id':r['threadId'],'packet_id':r['packet_id']} for r in dispatched],
      'development_qrels_ready':(DEV/'frozen/COMPLETE.json').is_file(),
      'development_base':get(DEV/'frozen/base/COMPLETE.json'),
      'development_grid':{k:v for k,v in (get(ROOT/'development-grid-r3/COMPLETE.json') or get(ROOT/'development-grid-r2/COMPLETE.json') or {}).items() if k in ['status','query_count','methods','rankings_sha256','scores_sha256','labels_read','test_access']},
      'selection_prepared':(ROOT/'selection/PREPARATION.json').is_file(),
      'test_sealed':(ROOT/'selection/SEALED.json').is_file(),
      'training_gates':[{'path':str(p),'sha256':sha(p),**{k:v for k,v in get(p).items() if k in ['status','triggered','selected_method','selected_error_query_count','qrels_sha256']}} for p in (ROOT/'training-preparation/gates').glob('*/gate.json')],
      'training_pool':{k:v for k,v in (get(ROOT/'training-pool/POOL_COMPLETE.json') or {}).items() if k in ['status','query_count','candidate_count','candidate_rows_sha256','binding_sha256']},
      'training_reviews_dispatched':len(list((ROOT/'training-review/dispatch').glob('*.json'))),
      'training_reviews_collected':len(list((ROOT/'training-review/reviews').glob('*/COLLECTED.json'))),
      'training_started':bool(list((ROOT/'training-preparation/runs').glob('*/config.json'))),
      'new_test_retrieval_complete':get(ROOT/'test-pool/POOL_COMPLETE.json'),
      'new_test_scored':(ROOT/'evaluation/final-test-v1/COMPLETE.json').is_file(),
      'agent_execution':[{'path':str(p),'sha256':sha(p),'status':get(p).get('status')} for p in (ROOT/'agent-execution-preparation').glob('*/COMPLETE.json')],
      'agent_quality_review':get(ROOT/'agent-quality-review-v2/frozen/COMPLETE.json'),
      'agent_quality_input_version':'v2_original_model_visible_scores_restored',
      'commerce_diagnosis':get(ROOT/'commerce-component-v1/DIAGNOSIS.json'),
      'final_audit_reconciliation':get(ROOT/'delivery-audit/FINAL_RECONCILIATION.json'),
      'closure_decision':get(ROOT/'delivery-closure-v1/COMPLETE.json'),
      'commerce_simulation':{k:v for k,v in (get(ROOT/'commerce-component-simulated-v1/COMPLETE.json') or {}).items() if k!='files'},
      'delivery_report':str(ROOT/'reports/delivery-overview-current.md') if (ROOT/'reports/delivery-overview-current.md').is_file() else None,
      'agent_verified':False,
      'code_readme':str(REPO/'experiments/search-closure-v1/README.md'),
      'completion_claim':'Only a final independently checked completion receipt may mark this goal complete.'}
    if status['training_pool']:status['stage']='training_independent_review'
    if status['training_started']:status['stage']='new_pairwise_training'
    if (ROOT/'training-preparation/TRAINING_CYCLE_DECIDED.json').is_file():status['stage']='final_development_topup_and_selection'
    if (ROOT/'training-preparation/new-models.json').is_file() and not (ROOT/'development-grid-r3/COMPLETE.json').is_file():status['stage']='new_model_development_grid'
    if (ROOT/'final-selection/SELECTION.json').is_file():status['stage']='frozen_test_and_agent_evaluation'
    if status['new_test_scored'] and status['agent_quality_review']:status['stage']='final_evidence_delivery_and_independent_audit'
    if status['final_audit_reconciliation']:status['stage']='commerce_verified_price_prerequisite'
    if status['closure_decision'] and status['closure_decision'].get('complete_under_latest_user_scope'):
        status.update(complete=True,goal_status='complete',stage='complete_with_user_approved_simulated_prices',
            delivery_report=str(ROOT/'reports/delivery-final.md'),agent_quality_review_completed=True,
            agent_quality_gate_passed=False,production_activated=False)
    path=ROOT/'STATUS.json';pending=path.with_suffix('.pending.json')
    pending.write_text(json.dumps(status,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');os.replace(pending,path)
    with (ROOT/'progress-events.jsonl').open('a',encoding='utf-8') as f:
        f.write(json.dumps(status,ensure_ascii=False)+'\n')
    print(json.dumps({k:status[k] for k in ['stage','reviews_dispatched','reviews_collected','review_pairs_collected','training_reviews_dispatched','training_reviews_collected','development_qrels_ready','test_sealed','training_started','new_test_scored','agent_verified']},ensure_ascii=False))

if __name__=='__main__':main()
