"""Higher-fidelity codes to reduce expensive original-vector reads."""
import build_ann as builder
builder.ANN_FOLDER='ann-v2'
builder.PQ_M=128
if __name__=='__main__':
    for source in ['multicpr','kuaisearch']:builder.build(source)
