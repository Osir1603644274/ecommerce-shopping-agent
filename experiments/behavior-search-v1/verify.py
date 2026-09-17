"""Recheck label construction, temporal partitions and score fixtures."""
import json,hashlib
import numpy as np
import ctr
D=ctr.DATA;c=json.loads((D/'config.json').read_text(encoding='utf8'));sessions={};stats={}
assert abs(ctr.auc(np.array([0,1,0,1]),np.array([.1,.5,.5,.9]))-.875)<1e-12
assert ctr.auc(np.array([0,1]),np.array([.5,.5]))==.5
for sp in ['train','dev','test']:
 x=np.load(D/(sp+'_x.npy'),mmap_mode='r');y=np.load(D/(sp+'_y.npy'),mmap_mode='r');groups=json.loads((D/(sp+'_groups.json')).read_text(encoding='utf8'));sessions[sp]=set();off=0;zero=0
 assert np.isfinite(x).all();assert set(np.unique(y))=={0,1}
 with (D/(sp+'.jsonl')).open(encoding='utf8') as f:
  for n,l in enumerate(f):
   r=json.loads(l);ids=r['impressed_item_ids'];clicked=set(r['clicked_item_ids']);actual=np.fromiter((i in clicked for i in ids),dtype=np.uint8);assert np.array_equal(y[off:off+len(ids)],actual);g=groups[n];assert g['start']==off and g['end']==off+len(ids) and g['sid']==r['session_id'];off+=len(ids);zero+=not clicked;sessions[sp].add(r['session_id']);assert r['time_index']>c['history_end']
   if sp=='train':assert r['split']=='train' and r['time_index']<=c['train_end'] and ctr.sampled(r['session_id'],16)
   if sp=='dev':assert r['split']=='train' and r['time_index']>c['train_end'] and ctr.sampled(r['session_id'],8)
   if sp=='test':assert r['split']=='test'
 assert off==len(y)==len(x);assert len(sessions[sp])==len(groups);stats[sp]={'requests':len(groups),'pairs':len(y),'clicks':int(y.sum()),'zero_click_requests':zero}
assert not(sessions['train']&sessions['dev'] or sessions['train']&sessions['test'] or sessions['dev']&sessions['test'])
assert stats['test']=={'requests':16541,'pairs':494271,'clicks':18770,'zero_click_requests':8085}
pair=np.load(D/'objective-v2/fixed_pairs.npz');y=np.load(D/'train_y.npy',mmap_mode='r');gg=json.loads((D/'train_groups.json').read_text());ends=np.array([g['end'] for g in gg]);assert (y[pair['positive']]==1).all() and (y[pair['negative']]==0).all();assert np.array_equal(np.searchsorted(ends,pair['positive'],side='right'),np.searchsorted(ends,pair['negative'],side='right'))
ctr.save('FINAL_VALIDATION.json',{'status':'PASS','all_sample_labels_rederived':True,'pairwise_same_request_verified':True,'auc_ties_verified':True,'time_disjoint_request_partitions':True,'stats':stats});print(json.dumps(stats))
