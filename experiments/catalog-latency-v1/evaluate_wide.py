"""Separate probe coverage from original-vector refinement count."""
import evaluate_fast as evaluation

evaluation.ARMS={
 'wide':{'kuaisearch':{'nprobe':512,'candidates':8192},'multicpr':{'nprobe':256,'candidates':4096}},
 'wide_refine':{'kuaisearch':{'nprobe':512,'candidates':16384},'multicpr':{'nprobe':256,'candidates':8192}},
 'all_lists':{'kuaisearch':{'nprobe':1024,'candidates':8192},'multicpr':{'nprobe':256,'candidates':4096}},
 'conservative':{'kuaisearch':{'nprobe':1024,'candidates':16384},'multicpr':{'nprobe':256,'candidates':8192}},
}

if __name__=='__main__':evaluation.main()
