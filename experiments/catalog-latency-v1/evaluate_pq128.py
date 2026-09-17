"""Frozen equal-query sweep for finer codes, fewer original-vector reads."""
import evaluate_fast as evaluation
from agent.app.catalog_fast_retrieval_v2 import FastRuntime
evaluation.FastRuntime=FastRuntime
evaluation.ARMS={
 'small':{'kuaisearch':{'nprobe':512,'candidates':2048},'multicpr':{'nprobe':256,'candidates':1024}},
 'middle':{'kuaisearch':{'nprobe':512,'candidates':4096},'multicpr':{'nprobe':256,'candidates':2048}},
 'wide':{'kuaisearch':{'nprobe':1024,'candidates':4096},'multicpr':{'nprobe':256,'candidates':2048}},
 'conservative':{'kuaisearch':{'nprobe':512,'candidates':8192},'multicpr':{'nprobe':256,'candidates':4096}},
}
if __name__=='__main__':evaluation.main()
