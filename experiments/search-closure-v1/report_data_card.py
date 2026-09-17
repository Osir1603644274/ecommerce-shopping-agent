"""Describe verified current data stages; never open heldout query contents."""
from collections import Counter
import json
from pathlib import Path
from retrieval_runtime import sha,read_json,write_once
from reviews import rows

ROOT=Path('D:/agent-datasets/search-closure-v1')
DEV=Path('D:/agent-datasets/search-stage1-dev-revision-v6')

def main():
    base=read_json(DEV/'frozen/base/COMPLETE.json');qpath=DEV/'frozen/base/qrels.jsonl'
    if sha(qpath)!=base['qrels_sha256']:raise ValueError('Base qrels changed')
    qrels=rows(qpath);grades=Counter(str(r['grade']) for r in qrels)
    seal=read_json(ROOT/'selection/SEALED.json');frozen=read_json(ROOT/'selection/FROZEN.json')
    if sha(ROOT/'selection/FROZEN.json')!=seal['frozen_sha256']:raise ValueError('Query seal changed')
    trainpath=ROOT/'selection/frozen/train.queries.jsonl'
    if sha(trainpath)!=frozen['files']['train.queries.jsonl']:raise ValueError('Training original queries changed')
    training=rows(trainpath);pool=read_json(ROOT/'training-pool/POOL_COMPLETE.json')
    gate=read_json(ROOT/'training-preparation/gates/v6-grid-r2/gate.json')
    examples=[]
    for source in ['kuaisearch','multicpr']:
        for r in sorted((r for r in training if r['source']==source),key=lambda r:r['query_id'])[:2]:
            native=r['native_origin']
            examples.append({'source':source,'query':r['text'],'original_query':native['original_query'],
                'native_split':r['native_split'],'source_file':native['path'],'source_line':native['source_line'],
                'source_row_sha256':native['source_row_sha256']})
    payload={'status':'BASE_AND_TRAINING_POOL_DATA_CARD_NOT_FINAL_BENCHMARK','base_qrels':base,
        'grade_counts':dict(grades),'source_grade_counts':{s:dict(Counter(str(r['grade']) for r in qrels if r['document_id'].split(':')[0]==s)) for s in ['kuaisearch','multicpr']},
        'original_train_query_examples':examples,'training_pool':pool,
        'input_files':{str(p):sha(p) for p in [qpath,trainpath,ROOT/'selection/SEALED.json',ROOT/'selection/FROZEN.json',ROOT/'training-pool/POOL_COMPLETE.json',ROOT/'training-preparation/gates/v6-grid-r2/gate.json']},
        'test_query_contents_read':False,'labels_generated':0}
    lines=['# 搜索数据卡：开发基础标签与新训练候选池','',
        '这是训练前的数据说明，不代表最终补审开发集、模型训练或新测试已经完成。','',
        '| 用途 | 查询 | 候选与标注 | 状态 |','|---|---:|---|---|',
        '| 开发基础集 | 40，两来源各20；33主评、7诊断 | 保留原有3,674对并按固定新规则重审 | 基础银标已冻结；最终Top10共同补审尚未进行 |',
        '| 新训练集 | 200，两来源各100 | 每query40个候选，共8,000对；独立A/B及分歧T复核 | 候选池已完成；以独立评审完整冻结为训练前提 |',
        '| 新项目测试集 | 80，两来源各40 | 最终配置冻结后才检索；每query80个候选 | 查询已隔离封存，当前不读取其候选或标签 |','',
        '搜索范围为两个来源的完整语料：KuaiSearch 6,634,118条，MultiCPR电商 1,002,822条。40/80是每个query送审的候选数，不是搜索商品库大小。','',
        '训练候选来自BM25、字符检索、稠密检索与预训练交叉编码重排各Top10并集，再以固定RRF补齐40。重排先对RRF召回的300个候选打分。缺失相关性判断不是负例。','',
        '## 查询与标签来源','',
        '新训练和项目留出查询取自本地已核验native train原始记录，保留原文与来源行。本项目重新划分它们，因此不称官方测试集。原生稀疏qrel没有直接当作新候选池的完整标签；本轮模型裁判逐对读取证据。旧开发查询保留其历史来源，不将修订标签称作重新采集真实query。','',
        '| 来源 | 实际训练query | 来源行 |','|---|---|---:|']
    for r in examples:lines.append('| '+r['source']+' | '+r['query'].replace('|','\\|')+' | '+str(r['source_line'])+' |')
    lines.extend(['','原始文件路径和行哈希见同名JSON证据文件；表格不公开会话或用户标识。','',
        '## 开发基础标签','',
        '| 等级 | 对数 | 含义 |','|---|---:|---|'])
    meanings={'0':'无关或核心用途不适用','1':'明确属性不符但仍可替代，或直接配件','2':'核心本体支持、无明确违背，但其他要求缺证','3':'所有明确要求均有证据支持','UNKNOWN':'意图、本体或影响等级的冲突无法确定'}
    for grade in ['0','1','2','3','UNKNOWN']:lines.append(f'| {grade} | {grades[grade]} | {meanings[grade]} |')
    lines.extend(['',f"本轮训练触发依据：实际23种开发排序中选出的旧配置 `{gate['selected_method']}`，在 {gate['selected_error_query_count']} 条主评query上存在已知更相关结果排在Top10外、较低等级占据Top10的反例，超过预设5条门槛。",'',
        '## 适用边界','',
        '- 标签是独立上下文模型银标，不是人工金标；同一模型可能有共同误差，评审一致率不能当准确率。',
        '- 评测为池化nDCG@10，并报告UNKNOWN界限、已判覆盖、池外和池内召回；不能宣称完整商品库的真实召回率。',
        '- query隔离包含原文/规范化去重与近邻语义复核，不保证消除一切潜在语义重合。',
        '- 商品标题、品牌、类别和已有卖家字段仅按原文使用；没有的价格、库存、适配条件不会补造。公开文档ID不等于商城可购买商品ID。',
        '- 该训练规模支持有限的本项目重排实验；是否有效由后续固定开发对照和一次项目留出测试决定。不能预先承诺提升。',''])
    out=ROOT/'reports/data-card-before-training'
    write_once(out.with_suffix('.json'),payload)
    text='\n'.join(lines);path=out.with_suffix('.md')
    if path.exists() and path.read_text(encoding='utf-8')!=text:raise ValueError('Immutable data card differs')
    if not path.exists():path.write_text(text,encoding='utf-8')
    print(json.dumps({'status':payload['status'],'path':str(path),'sha256':sha(path)},ensure_ascii=False))

if __name__=='__main__':main()
