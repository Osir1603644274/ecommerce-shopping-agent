"""Read-only Amazon recommendation tool. Namespaced source IDs never authorize commerce."""
from collections import Counter, defaultdict
from functools import lru_cache
import hashlib, html, itertools, json, math, os, re
from pathlib import Path
import numpy as np

SOURCE='amazon2018_luxury_beauty'

def fingerprint(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def tokens(text):return re.findall(r'[a-z0-9]{2,}',html.unescape(text).lower())

class RecommendationCatalog:
    def __init__(self,root):
        root=Path(root);self.root=root
        raw=(root/'BUNDLE.json').read_bytes();self.version=hashlib.sha256(raw).hexdigest()
        manifest=json.loads(raw)
        if manifest['source']!=SOURCE or manifest['commerceAuthority'] is not False:raise ValueError('invalid_recommendation_bundle')
        for name,expected in manifest['files'].items():
            if Path(name).name!=name or hashlib.sha256((root/name).read_bytes()).hexdigest()!=expected:
                raise ValueError('recommendation_bundle_changed')
        self.catalog=sorted([json.loads(line) for line in (root/'catalog.jsonl').read_text(encoding='utf-8').splitlines()],key=lambda p:p['item_id'])
        self.ids=[p['item_id'] for p in self.catalog];self.index={i:n for n,i in enumerate(self.ids)}
        if len(self.index)!=len(self.ids):raise ValueError('duplicate_source_identity')
        if self.ids!=json.loads((root/'item_ids.json').read_text()):raise ValueError('embedding_identity_mismatch')
        self.vectors=np.load(root/'vectors.npy',allow_pickle=False,mmap_mode='r')
        self.fit=json.loads((root/'fit_artifacts.json').read_text());self.trees=json.loads((root/'trees.json').read_text())['tree_info']
        self.cases={c['caseId']:c for c in json.loads((root/'cases.json').read_text())}
        self.case_digest=fingerprint(self.cases)
        counts=self.fit['positive_item_user_counts'];co=defaultdict(Counter)
        for items in self.fit['user_positive_items'].values():
            for a,b in itertools.combinations(sorted(set(items)&self.index.keys()),2):co[a][b]+=1;co[b][a]+=1
        self.cf={a:{b:v/math.sqrt(counts[a]*counts[b]) for b,v in row.items()} for a,row in co.items()}
        docs=[Counter(tokens(' '.join(str(p.get(k) or '') for k in ['title','brand','category']))) for p in self.catalog]
        df=Counter(t for doc in docs for t in doc);self.idf={t:math.log((1+len(docs))/(1+c))+1 for t,c in df.items()}
        self.tv={};self.postings=defaultdict(list)
        for item,doc in zip(self.ids,docs):
            v={t:(1+math.log(c))*self.idf[t] for t,c in doc.items()};norm=math.sqrt(sum(w*w for w in v.values()))
            self.tv[item]={t:w/norm for t,w in v.items()} if norm else {}
            for t,w in self.tv[item].items():self.postings[t].append((item,w))

    def case(self,case_id):
        if case_id=='cold':return {'caseId':'cold','history':[],'seen':[],'cutoff':0}
        if case_id not in self.cases:raise ValueError('unknown_demo_case')
        return self.cases[case_id]

    def product(self,item):
        p=self.catalog[self.index[item]]
        return {'source':SOURCE,'sourceItemId':item,'title':p['title'],'brand':p['brand'],'category':p['category'],
                'price':None,'currency':None,'commerceAuthority':False,'recordSha256':fingerprint(p)}

    def top(self,scores,seen,k=100):
        return sorted((i for i,v in scores.items() if i in self.index and i not in seen and math.isfinite(v) and v>0),key=lambda i:(-scores[i],i))[:k]

    def tree_scores(self,x):
        # LightGBM compares float32 features, converted to double, to double thresholds.
        # Explicit conversion avoids NumPy array scalar promotion rounding a split boundary.
        values=np.asarray(x,dtype=np.float64);scores=np.zeros(len(values),dtype=np.float64)
        def visit(node,indices):
            if not len(indices):return
            if 'leaf_value' in node:scores[indices]+=node['leaf_value'];return
            if node['decision_type']!='<=':raise ValueError('unsupported_tree_split')
            left=values[indices,node['split_feature']]<=node['threshold']
            visit(node['left_child'],indices[left]);visit(node['right_child'],indices[~left])
        for tree in self.trees:visit(tree['tree_structure'],np.arange(len(values)))
        return scores.tolist()

    def recommend(self,history,seen,*,limit=10,excluded=(),query=''):
        if not 1<=limit<=100:raise ValueError('invalid_limit')
        seen=set(seen)|set(excluded)
        if not seen<=self.index.keys():
            # Missing-metadata historical identities are allowed, but cannot become results.
            seen=seen&self.index.keys()
        known=sorted({h['item_id'] for h in history if h['item_id'] in self.index});hi=[self.index[i] for i in known]
        cfscore=Counter();tq=Counter()
        for i in known:cfscore.update(self.cf.get(i,{}));tq.update(self.tv[i])
        if query:
            tq={t:(1+math.log(c))*self.idf.get(t,0) for t,c in Counter(tokens(query)).items()}
        norm=math.sqrt(sum(v*v for v in tq.values()));ts=Counter()
        if norm:
            for t,v in tq.items():
                for i,w in self.postings[t]:ts[i]+=v/norm*w
        if query:
            listing=self.top(ts,seen,limit)
            return self.result(listing,history,seen,'text_search_tfidf',None,[ts[i] for i in listing])
        if not hi:
            listing=self.top(self.fit['positive_item_user_counts'],seen,limit)
            return self.result(listing,history,seen,'popular','no_supported_positive_history',[self.fit['positive_item_user_counts'][i] for i in listing])
        mean=self.vectors[hi].mean(0);mean/=max(np.linalg.norm(mean),1e-12)
        ns=self.vectors@mean
        cf=self.top(cfscore,seen);tf=self.top(ts,seen);neural=self.top(dict(zip(self.ids,ns)),seen)
        candidates=sorted(set(cf)|set(tf)|set(neural));ci=[self.index[i] for i in candidates]
        if not candidates:return self.result([],history,seen,'lambdamart_0','no_remaining_candidates',[])
        recent=max(h['timestamp'] for h in history)
        ri=sorted({self.index[h['item_id']] for h in history if h['timestamp']==recent and h['item_id'] in self.index})
        maxsim=(self.vectors[ci]@self.vectors[hi].T).max(1)
        rec=(self.vectors[ci]@self.vectors[ri].mean(0)) if ri else np.zeros(len(ci))
        brands=Counter(self.catalog[i]['brand'].lower() for i in hi if self.catalog[i]['brand'])
        cats=Counter(self.catalog[i]['category'].lower() for i in hi if self.catalog[i]['category'])
        ranks=[{i:1/(60+n) for n,i in enumerate(lst,1)} for lst in [cf,neural,tf]]
        matrix=[]
        for n,(item,i) in enumerate(zip(candidates,ci)):
            p=self.catalog[i];count=self.fit['positive_item_user_counts'].get(item,0)
            matrix.append([math.log1p(count),cfscore[item],float(ns[i]),float(maxsim[n]),float(rec[n]),
                 brands[p['brand'].lower()]/len(hi) if p['brand'] else 0,cats[p['category'].lower()]/len(hi) if p['category'] else 0,
                 math.log1p(len(hi)),int(count==0),*[r.get(item,0) for r in ranks]])
        values=self.tree_scores(np.asarray(matrix,dtype=np.float32))
        ordered=sorted(range(len(candidates)),key=lambda n:(-values[n],candidates[n]))[:limit]
        return self.result([candidates[n] for n in ordered],history,seen,'lambdamart_0',None,[values[n] for n in ordered])

    def result(self,listing,history,seen,strategy,fallback,scores):
        result={'source':SOURCE,'modelVersion':self.version,'historySha256':fingerprint(history),
                'seenSha256':fingerprint(sorted(seen)),'strategy':strategy,'fallback':fallback,'commerceAuthority':False,
                'items':[{**self.product(i),'rank':n,'score':float(score)} for n,(i,score) in enumerate(zip(listing,scores),1)]}
        result['scopeId']=fingerprint(result);return result

@lru_cache(maxsize=1)
def get_recommendation_catalog():
    root=os.environ.get('RECOMMENDATION_BUNDLE_DIR','D:/agent-datasets/recommendation-completion-v1/serving-v1')
    return RecommendationCatalog(root)
