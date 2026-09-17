"""Expose every registered final selection to Agent without changing old runtime."""
import hashlib
from pathlib import Path
import retrieval_runtime as runtime

class AgentRetrievalRuntime(runtime.RetrievalRuntime):
    def strategy_manifest(self,*,profile='w111',model_path=None):
        if profile not in runtime.CHANNELS:
            return super().strategy_manifest(profile=profile,model_path=model_path)
        if model_path is not None:raise ValueError('Raw retrieval cannot have a CE model')
        return {'version':'search-closure-raw-agent-provider-v1','dataRoot':str(self.root),
            'assetAuditSha256':self.asset['audit_sha256'],'profile':profile,'depth':runtime.DEPTH,
            'model':None,'inputText':'catalog.documents.text','queryInstruction':runtime.QUERY_INSTRUCTION,
            'runtimeCodeSha256':runtime.sha(Path(runtime.__file__)),'adapterCodeSha256':runtime.sha(Path(__file__))}

    def search_one(self,query,source,*,profile='w111',model_path=None,limit=10):
        if profile not in runtime.CHANNELS:
            return super().search_one(query,source,profile=profile,model_path=model_path,limit=limit)
        if model_path is not None:raise ValueError('Raw retrieval cannot have a CE model')
        if type(limit) is not int or not 1<=limit<=20:raise ValueError('Invalid result limit')
        query_id='agent-'+runtime.fingerprint({'source':source,'query':query})[:20]
        recalled=self.retrieve_batch(source,[{'query_id':query_id,'query':query}],include_dense=profile=='dense')
        selected=recalled['channels'][query_id][profile][:limit]
        texts=self.document_texts(source,[r['document_id'] for r in selected])
        hits=[{'source':source,'docid':r['document_id'],'text':texts[r['document_id']],
            'rank':r['rank'],'score':r['score'],'unknown':[],
            'provenance':{'catalog':str(self.root/'catalog.sqlite'),
                'catalogSha256':self.asset['files'][str(self.root/'catalog.sqlite')]['sha256'],
                'textSha256':hashlib.sha256(texts[r['document_id']].encode()).hexdigest(),
                'profile':profile,'retrievalDepth':runtime.DEPTH,'ceModel':None}} for r in selected]
        return {'hits':hits,'timings':{'recall':recalled['timings'],'ce':None}}
