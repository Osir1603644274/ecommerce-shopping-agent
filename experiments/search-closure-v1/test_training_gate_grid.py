from argparse import Namespace
import copy
from pathlib import Path
import pytest
import train_pairwise as t
from metrics_v2 import canonical_hash

def data():
    queries={q:{'query_id':q,'query':q,'source':s,'query_cohort':'main'} for q,s in [('q-ku','kuaisearch'),('q-mu','multicpr')]}
    labels={q:{r['source']+':high':3,r['source']+':low':0} for q,r in queries.items()}
    methods=sorted({'bm25','character','dense'}|set(t.GRID_CE_METHODS)|{p+'/none' for p in t.GRID_PROFILES})
    rankings={m:{q:[r['source']+':low',r['source']+':high'] for q,r in queries.items()} for m in methods}
    return queries,labels,rankings

def test_profile_strength_and_predeclared_tie_order():
    queries,labels,rankings=data()
    for m in ('w211/epoch2','w112/epoch2','w111/epoch3'):
        rankings[m]={q:list(reversed(r)) for q,r in rankings[m].items()}
    result=t.select_old_ce(queries,rankings,labels,t.GRID_CE_METHODS)
    assert result['selected_method']=='w211/epoch2'
    assert len(result['candidates'])==16
    for m in rankings:rankings[m]=copy.deepcopy(rankings['w211/epoch2'])
    assert t.select_old_ce(queries,rankings,labels,t.GRID_CE_METHODS)['selected_method']=='w111/base'

def fixture(tmp_path,monkeypatch):
    monkeypatch.setattr(t,'DATA',tmp_path)
    monkeypatch.setattr(t.evaluation,'safe_input',lambda p:Path(p).resolve())
    queries,_,rankings=data();grid=tmp_path/'grid';wanted='a'*64
    complete={'status':'COMPLETE','query_count':40,'test_access':False,'labels_read':False,
        'methods':list(rankings),'binding_sha256':'b'*64,'rankings_sha256':wanted,'scores_sha256':'c'*64,'files':[]}
    binding={'models':{m:{'path':str(t.OLD/('models/reranker-'+t.REVISION) if m=='base' else t.OLD/'training/run-lora-v1'/('epoch-'+m[-1]))} for m in ('base','epoch1','epoch2','epoch3')},
        'queries_sha256':t.evaluation.FIXED_QUERY_METADATA_SHA256,'test_access':False,'labels_read':False,
        'depth':300,'rrf_k':60,'weights':{'w111':[1,1,1],'w211':[2,1,1],'w112':[1,1,2],'no_dense':[1,1,0]},'code':{}}
    rows=[{'query_id':q,'source':queries[q]['source'],'method':m,'ranking':r,'ranking_sha256':canonical_hash(r)} for m,qr in rankings.items() for q,r in qr.items()]
    class Evidence:
        def json(self,path,expected=None):return complete if Path(path).name=='COMPLETE.json' else binding
        def file(self,path,expected=None):return wanted
        def rows(self,path,expected=None):return rows
    return Namespace(rankings=grid/'rankings.jsonl',rankings_sha256=wanted),Evidence(),queries,complete,binding,rows

def test_grid_source_records_all_23_before_selecting_16(tmp_path,monkeypatch):
    args,evidence,queries,*_=fixture(tmp_path,monkeypatch)
    ranks,methods,proof=t.load_gate_rankings(args,evidence,queries)
    assert len(ranks)==23 and methods==t.GRID_CE_METHODS
    assert proof['kind']=='current_full_index_23_config_grid'

@pytest.mark.parametrize('mutation',['new_model','query_hash','ranking_hash','query_membership','missing_method'])
def test_wrong_grid_rejected(tmp_path,monkeypatch,mutation):
    args,evidence,queries,complete,binding,rows=fixture(tmp_path,monkeypatch)
    if mutation=='new_model':binding['models']['new_epoch1']={'path':'wrong'}
    elif mutation=='query_hash':binding['queries_sha256']='d'*64
    elif mutation=='ranking_hash':complete['rankings_sha256']='d'*64
    elif mutation=='query_membership':rows[0]['query_id']='heldout'
    else:complete['methods'].pop()
    with pytest.raises(ValueError):t.load_gate_rankings(args,evidence,queries)
