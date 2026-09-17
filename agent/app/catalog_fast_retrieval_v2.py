"""Higher-precision PQ codes; same original-vector and CE refinement."""
from collections import OrderedDict
from pathlib import Path
import sys
import numpy as np
from .catalog_fast_retrieval import FastRuntime as ParentRuntime,old,DEFAULT


class FastRuntime(ParentRuntime):
    def __init__(self,*,directory=DEFAULT,parameters=None,progress=None):
        old.RetrievalRuntime.__init__(self,progress=progress)
        self.directory=Path(directory);sys.path.insert(0,str(self.directory/'deps'))
        import faiss
        self.faiss=faiss;faiss.omp_set_num_threads(4)
        self.parameters=parameters or old.read_json(self.directory/'selected-v2.json')['parameters']
        self.ann={};self.ann_receipts={};self.vectors={};self.query_cache=OrderedDict()
        for source in old.SOURCES:
            path=self.directory/'ann-v2'/source/'COMPLETE.json';receipt=old.read_json(path)
            indexfile=path.parent/receipt['file'];original=self.dense_manifests[source]
            if (receipt['status']!='COMPLETE' or receipt['config']['pqM']!=128
                or receipt['config']['sourceSha256']!=original['vectors_sha256']
                or receipt['config']['documents']!=original['binding']['documents'] or old.sha(indexfile)!=receipt['sha256']):
                raise ValueError('ann_source_binding_mismatch')
            index=faiss.read_index(str(indexfile))
            if index.ntotal!=original['binding']['documents'] or index.d!=512:raise ValueError('ann_count_dimension')
            self.ann[source]=index;self.ann_receipts[source]={'sha256':old.sha(path),'indexSha256':receipt['sha256']}
            self.vectors[source]=np.load(self.root/'indexes'/source/'embeddings.npy',mmap_mode='r')
            self.indexes[source].execute('PRAGMA cache_size=-16384')
        self.catalog.execute('PRAGMA cache_size=-16384')

    def strategy_manifest(self,*,profile='w211',model_path=None):
        result=super().strategy_manifest(profile=profile,model_path=model_path)
        result.update(version='catalog-indexed-online-v2',fastCodeSha256=old.sha(__file__),
            parentFastCodeSha256=old.sha(Path(__file__).with_name('catalog_fast_retrieval.py')),annVariant='ann-v2')
        return result
