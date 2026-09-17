from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re
import threading
from typing import Any


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding='utf8').splitlines() if line.strip()]


def tokens(text: str) -> list[str]:
    parts = re.findall(r'[a-z0-9]+|[\u4e00-\u9fff]+', text.lower())
    return [t for p in parts for t in ([p] if p.isascii() else list(p)+[p[i:i+2] for i in range(len(p)-1)])]


class KnowledgeStore:
    """Immutable snapshot. Exact model filtering precedes BOTH retrieval channels.

    No application state, catalogue mutation, HTTP fetch, or LLM inside this store.
    An unavailable embedding channel is reported, never presented as hybrid success.
    """
    def __init__(self, directory: str | Path, *, embedder: Any = None):
        self.directory = Path(directory)
        self.manifest = json.loads((self.directory/'manifest.json').read_text(encoding='utf8'))
        for name, digest in self.manifest['files'].items():
            if hashlib.sha256((self.directory/name).read_bytes()).hexdigest()!=digest:
                raise ValueError('knowledge_snapshot_hash_mismatch')
        self.version = self.manifest['version']
        self.facts = read_jsonl(self.directory/'facts.jsonl')
        self.by_id = {r['evidenceId']:r for r in self.facts}
        if len(self.by_id)!=len(self.facts): raise ValueError('duplicate_evidence_id')
        self.listings = {r['itemId']:r for r in read_jsonl(self.directory/'listings.jsonl')}
        self.models = {r['modelKey']:r for r in read_jsonl(self.directory/'models.jsonl')}
        self.sources = {r['sourceId']:r for r in read_jsonl(self.directory/'sources.jsonl')}
        if any(r['sourceId'] not in self.sources for r in self.facts): raise ValueError('missing_source')
        self.embedder = embedder
        self._embedding_lock = threading.Lock()
        self._vectors: dict[str, Any] = {}
        self._terms = {r['evidenceId']:Counter(tokens(r['text'])) for r in self.facts}

    def evidence(self, evidence_id: str) -> dict:
        fact = self.by_id.get(evidence_id)
        if fact is None: return dict(status='NOT_FOUND',knowledgeVersion=self.version,evidence=None)
        return dict(status='FOUND',knowledgeVersion=self.version,evidence={**fact,'source':self.sources[fact['sourceId']]})

    def query_models(self, query: str) -> set[str]:
        norm=re.sub(r'\W','',query).lower()
        def aliases(model):
            name=re.sub(r'\W','',model['displayName']).lower()
            brand,suffix=model['modelKey'].split(':',1)
            chinese={'apple':'苹果','huawei':'华为','honor':'荣耀','samsung':'三星','xiaomi':'小米','redmi':'红米'}.get(brand)
            return [name,name.removeprefix('apple'),brand+suffix,*([chinese+suffix] if chinese else [])]
        matches=[m for m in self.models.values() if any(a in norm for a in aliases(m)) or m['modelKey'] in query.lower()]
        return {m['modelKey'] for m in matches if not any(
            m['modelKey']!=o['modelKey'] and m['modelKey'].split(':')[0]==o['modelKey'].split(':')[0]
            and o['modelKey'].split(':')[1].startswith(m['modelKey'].split(':')[1]) for o in matches)}

    def _rank(self, query: str, facts: list[dict]) -> tuple[list[dict], dict]:
        qt=Counter(tokens(query)); n=len(facts)
        if not n: return [],dict(bm25='EMPTY',bge='EMPTY')
        lengths=[sum(self._terms[f['evidenceId']].values()) for f in facts]
        avg=sum(lengths)/n or 1
        df=Counter(t for f in facts for t in self._terms[f['evidenceId']])
        scores={}
        for f,length in zip(facts,lengths):
            counts=self._terms[f['evidenceId']]
            scores[f['evidenceId']]=sum(qc*math.log(1+(n-df[t]+.5)/(df[t]+.5))*counts[t]*2.2/
                (counts[t]+1.2*(.25+.75*length/avg)) for t,qc in qt.items() if counts[t])
        bm=sorted(facts,key=lambda f:(-scores[f['evidenceId']],f['evidenceId']))
        channels=[bm]; status=dict(bm25='OK',bge='UNAVAILABLE')
        if self.embedder is not None:
            try:
                import numpy as np
                with self._embedding_lock:
                    missing=[f for f in facts if f['evidenceId'] not in self._vectors]
                    if missing:
                        vectors=list(self.embedder.embed([f['text'] for f in missing]))
                        if len(vectors)!=len(missing): raise ValueError('embedding_count')
                        for f,v in zip(missing,vectors): self._vectors[f['evidenceId']]=np.asarray(v)
                    q=np.asarray(next(iter(self.embedder.embed(['为这个句子生成表示以用于检索相关文章：'+query]))))
                    sim={f['evidenceId']:float(np.dot(q,self._vectors[f['evidenceId']])/
                        max(float(np.linalg.norm(q)*np.linalg.norm(self._vectors[f['evidenceId']])),1e-12)) for f in facts}
                channels.append(sorted(facts,key=lambda f:(-sim[f['evidenceId']],f['evidenceId'])))
                status['bge']='OK'
            except Exception:
                status['bge']='FAILED'
        rrf=Counter()
        for channel in channels:
            for rank,f in enumerate(channel,1): rrf[f['evidenceId']]+=1/(60+rank)
        return sorted(facts,key=lambda f:(-rrf[f['evidenceId']],f['evidenceId'])),status

    def search(self, query: str, *, item_ids: list[str] | None = None,
               model_keys: list[str] | None = None, fields: list[str] | None = None, top_k: int = 8, context_query: str | None = None) -> dict:
        if not isinstance(query,str) or not 1<=len(query.strip())<=2000: raise ValueError('invalid_query')
        if context_query is not None and (not isinstance(context_query,str) or not 1<=len(context_query.strip())<=2000):raise ValueError('invalid_context_query')
        if type(top_k) is not int or not 1<=top_k<=20: raise ValueError('invalid_top_k')
        ids=item_ids or []; keys=model_keys or []
        if len(ids)>20 or len(keys)>20 or any(not isinstance(i,str) for i in ids+keys): raise ValueError('invalid_scope')
        if fields is not None and (not isinstance(fields,list) or any(f not in {
            'chip','camera','screen','battery','charging','gaming_test','camera_test','battery_test'
        } for f in fields)): raise ValueError('invalid_fields')
        selected=set(); bindings=[]; gaps=[]
        for iid in dict.fromkeys(ids):
            r=self.listings.get(iid)
            if not r:
                gaps.append(dict(itemId=iid,reason='ITEM_NOT_IN_SNAPSHOT')); continue
            bindings.append(r)
            if r['status']=='CLEAR':
                selected.update(r['modelKeys'])
                if r['region']=='UNKNOWN': gaps.append(dict(itemId=iid,reason='LISTING_REGION_UNVERIFIED'))
                if r.get('variantStatus','').startswith('UNVERIFIED'):
                    gaps.append(dict(itemId=iid,reason='LISTING_VARIANT_UNVERIFIED'))
            else: gaps.append(dict(itemId=iid,reason='MODEL_'+r['status']))
        for key in keys:
            if key in self.models: selected.add(key)
            else: gaps.append(dict(modelKey=key,reason='MODEL_NOT_IN_SNAPSHOT'))
        # A scoped lookup must NEVER fall back to the whole collection.
        if not ids and not keys:
            selected=self.query_models(query)
            if not selected: gaps.append(dict(reason='MODEL_REQUIRED',message='请提供型号或商品ID'))
        requested=self.query_models(query) or (self.query_models(context_query) if context_query else set())
        if requested:
            missing=requested-selected
            for key in sorted(missing):
                gaps.append(dict(modelKey=key,reason='REQUESTED_MODEL_NOT_IN_CANDIDATES'))
                gaps.extend(dict(modelKey=key,**gap) for gap in self.models[key].get('gaps',[]))
            selected=selected & requested
        if fields is None:
            positive=re.sub(r'不(?:怎么)?(?:打|玩|考虑)游戏|不(?:要求|重视|考虑)拍照','',query)
            hints=[('芯片|处理器',['chip']),('相机|拍照|摄影',['camera','camera_test']),('续航',['battery','battery_test']),
                   ('电池',['battery','battery_test']),('充电',['charging']),('屏幕',['screen']),('游戏|帧率|性能',['chip','gaming_test'])]
            fields=list(dict.fromkeys(f for pattern,fs in hints if re.search(pattern,positive) for f in fs)) or None
        facts=[f for f in self.facts if f['modelKey'] in selected and (not fields or f['field'] in fields)]
        for key in sorted(selected):
            if not any(f['modelKey']==key for f in facts): gaps.append(dict(modelKey=key,reason='NO_VERIFIED_FACTS'))
            for gap in self.models[key].get('gaps',[]):
                if not fields or gap['field'] in fields: gaps.append(dict(modelKey=key,**gap))
        # A known region mismatch is excluded, not described as the listing's specification.
        if ids and not keys:
            safe=[]
            for fact in facts:
                region=self.sources[fact['sourceId']].get('region','UNKNOWN')
                matched=[b for b in bindings if b['status']=='CLEAR' and fact['modelKey'] in b['modelKeys']]
                if matched and all(b['region'] not in {'UNKNOWN',region} for b in matched) and region not in {'UNKNOWN','TEST_SAMPLE'}:
                    gaps.append(dict(modelKey=fact['modelKey'],evidenceId=fact['evidenceId'],reason='REGION_MISMATCH'))
                else: safe.append(fact)
            facts=safe
        ranked,channels=self._rank(query,facts)
        # Round robin by requested model prevents one well-documented model crowding out another.
        picked=[]
        for key in sorted(selected):
            first=next((r for r in ranked if r['modelKey']==key),None)
            if first and len(picked)<top_k: picked.append(first)
        picked.extend(r for r in ranked if r not in picked)
        picked=picked[:top_k]
        return dict(knowledgeVersion=self.version,status='FOUND' if picked else 'INSUFFICIENT_EVIDENCE',
            modelKeys=sorted(selected),requestedModelKeys=sorted(requested),bindings=bindings,
            evidence=[self.evidence(f['evidenceId'])['evidence'] for f in picked],gaps=gaps,
            retrieval=dict(channels=channels,modelFilterApplied=True,fields=fields or [],fusion='RRF_K60',topK=top_k),
            notice='型号资料仅作注明版本的参考；未核验卖家实物，不证明机况、当前价格或库存。')
