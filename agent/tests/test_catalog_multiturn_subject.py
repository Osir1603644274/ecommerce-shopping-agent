"""Identity, grounded filtering, and persisted presentation regression boundaries."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import catalog_conversation as c
from app.catalog_service import document_scope, fingerprint, verify_scope


def fixture_scope():
    sources=[]
    for source in ('kuaisearch','multicpr'):
        titles=['同标题低脂面包','花生酱三明治拌面面包减脂蘸料'] if source=='kuaisearch' else ['同标题低脂面包']
        hits=[];metadata=[]
        for i,title in enumerate(titles,1):
            docid=f'{source}:{i}'
            hits.append(dict(source=source,docid=docid,rank=i,score=5-i))
            metadata.append(dict(docid=docid,titleGroupKey=title,fields={
                'title':{'value':title},'seller':{'value':f'{source}店'},'brand':{'value':None}},
                provenance={'recordSha256':'a'*64}))
        sources.append(dict(source=source,hits=hits,metadata=metadata))
    return document_scope('低脂面包',sources,'b'*64,[dict(facet='商品',mode='require',value='面包',terms=[])])


def reviews(scope):
    return [dict(number=g['number'],relation='other' if '花生酱' in g['title'] else 'target',
                 quote='花生酱' if '花生酱' in g['title'] else '面包',reason='按标题商品主体判断') for g in scope['groups']]


def test_same_title_different_ids_are_distinct_and_sellers_are_visible():
    scope=fixture_scope()
    assert len(scope['groups'])==3
    assert [g['members'][0]['docid'] for g in scope['groups'][:2]]==['kuaisearch:1','multicpr:1']
    text=c.render_documents(scope)
    assert text.count('同标题低脂面包')==2 and '卖家：kuaisearch店' in text


def test_subject_rejection_keeps_original_evidence_and_rebinds_display():
    scope=fixture_scope();before=deepcopy(scope)
    reviewed=c.apply_subject_review(scope,reviews(scope))
    verify_scope(reviewed)
    assert scope==before and reviewed['sources']==scope['sources']
    assert len(reviewed['groups'])==2 and len(reviewed['semanticExcludedGroups'])==1
    assert reviewed['subjectReviewBaseScopeId']==scope['scopeId']
    assert all('花生酱' not in g['title'] for g in reviewed['groups'])
    assert '花生酱' not in c.render_documents(reviewed)


def test_unknown_properties_do_not_remove_candidates():
    scope=fixture_scope();r=reviews(scope)
    for item in r:item.update(relation='unknown',quote='',reason='未提供配料')
    assert len(c.apply_subject_review(scope,r)['groups'])==3


def test_negative_soft_preference_can_demote_but_never_filter():
    from app.catalog_requirements import select_groups
    groups=[dict(title='全麦面包',members=[]),dict(title='法式面包',members=[])]
    good,bad=select_groups(groups,[dict(facet='原料',mode='avoid',value='全麦',terms=['全麦'])])
    assert len(good)==2 and not bad and good[0]['title']=='法式面包'


def test_state_summary_cannot_turn_avoid_into_a_hard_exclusion():
    summary=c.requirement_summary([dict(facet='商品',mode='require',value='食品'),
        dict(facet='口感',mode='prefer',value='好吃'),dict(facet='原料',mode='avoid',value='全麦')])
    assert summary=='食品；优先好吃（非必须）；尽量避开全麦（非必须）'
    assert '不要全麦' not in summary and '面包' not in summary


def test_same_subject_with_quoted_hard_conflict_is_excluded_but_missing_is_not():
    scope=fixture_scope();scope['groups'][0]['title']='无糖可乐330ml';scope['groups'][1]['title']='无糖可乐容量待核验'
    scope['groups']=scope['groups'][:2]
    scope['requirements']=[dict(facet='商品',mode='require',value='可乐'),dict(facet='容量',mode='require',value='888ml')]
    scope['scopeId']=fingerprint({k:v for k,v in scope.items() if k!='scopeId'})
    r=[dict(number=g['number'],relation='target',quote='可乐',reason='标题为可乐',conflicts=[]) for g in scope['groups']]
    r[0]['conflicts']=[dict(facet='容量',value='888ml',quote='330ml',reason='明确容量不同')]
    result=c.apply_subject_review(scope,r)
    assert len(result['groups'])==1 and '待核验' in result['groups'][0]['title']
    scope['requirements'][1]['mode']='avoid';scope['scopeId']=fingerprint({k:v for k,v in scope.items() if k!='scopeId'})
    soft_result=c.apply_subject_review(scope,r)
    assert len(soft_result['groups'])==2 and not soft_result['semanticExcludedGroups']
    reviewed=next(g for g in soft_result['groups'] if '330ml' in g['title'])
    assert not reviewed['subjectReview']['conflicts']
    assert reviewed['subjectReview']['ignoredConflicts'][0]['validation']=='soft_preference_not_a_hard_filter'
    assert r[0]['conflicts']  # Preserve original model output for audit.
    r[0]['conflicts'][0]['facet']='虚构条件'
    with pytest.raises(ValueError,match='not_hard'):c.apply_subject_review(scope,r)


def test_valid_item_beyond_initial_six_survives_review_before_display_limit():
    sources=[]
    for source in ('kuaisearch','multicpr'):
        hits=[];metadata=[]
        for i in range(1,8):
            docid=f'{source}:{i}'
            title='可口可乐无糖888ml' if source=='kuaisearch' and i==7 else '无糖可乐330ml'
            hits.append(dict(source=source,docid=docid,rank=i,score=8-i))
            metadata.append(dict(docid=docid,titleGroupKey=title,fields={
                'title':{'value':title},'brand':{'value':None},'seller':{'value':None}},provenance={'recordSha256':'a'*64}))
        sources.append(dict(source=source,hits=hits,metadata=metadata))
    scope=document_scope('无糖可乐888ml',sources,'b'*64,[dict(facet='商品',mode='require',value='无糖可乐',terms=[]),
        dict(facet='容量',mode='require',value='888ml',terms=[])])
    assert len(scope['groups'])==14 and not any('888' in g['title'] for g in scope['groups'][:6])
    r=[dict(number=g['number'],relation='target',quote='可乐',reason='标题商品主体',
        conflicts=[] if '888' in g['title'] else [dict(facet='容量',value='888ml',quote='330ml',reason='容量不同')]) for g in scope['groups']]
    result=c.apply_subject_review(scope,r)
    assert len(result['groups'])==1 and result['groups'][0]['members'][0]['docid']=='kuaisearch:7'
    assert len(result['semanticExcludedGroups'])==13


@pytest.mark.parametrize('fault',['foreign','duplicate'])
def test_subject_review_cannot_invent_identity_or_source_quote(fault):
    scope=fixture_scope();r=reviews(scope)
    if fault=='foreign':r[0]['number']=999
    elif fault=='duplicate':r[1]['number']=r[0]['number']
    with pytest.raises(ValueError):c.apply_subject_review(scope,r)


def test_unquoted_subject_is_retained_as_unknown_without_adopting_model_claim():
    scope=fixture_scope();r=reviews(scope)
    r[0].update(relation='other',quote='未经提供的配料表')
    result=c.apply_subject_review(scope,r)
    kept=next(g for g in result['groups'] if g['subjectReview']['number']==r[0]['number'])
    assert kept['subjectReview']['relation']=='unknown'
    assert kept['subjectReview']['ignoredSubjectReview']['quote']=='未经提供的配料表'
    assert r[0]['relation']=='other'


def test_answer_filters_exact_scope_used_for_listing_and_next_turn(monkeypatch):
    scope=fixture_scope()
    output=dict(answer='候选标题宣称低脂，营养成分与口感尚待核验。',subjectReviews=reviews(scope))
    response=SimpleNamespace(tool_calls=[SimpleNamespace(function=SimpleNamespace(
        name='answer_catalog_candidates',arguments=json.dumps(output)))])
    monkeypatch.setattr(c,'model_call',AsyncMock(return_value=(response,{})))
    current=dict(query='低脂面包',requirements=scope['requirements'],scope=scope)
    answer,receipt=asyncio.run(c.answer_turn('找面包',dict(action='refine',numbers=[]),current))
    assert '花生酱' not in answer and answer.count('同标题低脂面包')==2
    applied=c.apply_subject_review(scope,receipt['scopeReview']['reviews'])
    assert '花生酱' not in str(c.compact_evidence(applied))
    assert receipt['scopeReview']['baseScopeId']==scope['scopeId']


def test_broaden_and_exclude_are_different_reversible_state_changes():
    current=dict(query='面包；优先口感好',retrievalQuery='面包',revision=1,history=[],scope=fixture_scope(),
        requirements=[dict(facet='商品',mode='require',value='面包',terms=[]),
                      dict(facet='口感',mode='prefer',value='口感好',terms=[])])
    broad=[dict(facet='商品',mode='require',value='食品',terms=[]),current['requirements'][1]]
    widened,_=c.transition(current,dict(action='refine',query='食品；优先口感好',retrievalQuery='食品',requirements=broad))
    excluded,_=c.transition(widened,dict(action='refine',query='食品，不要面包',retrievalQuery='食品',
        requirements=broad+[dict(facet='商品',mode='exclude',value='面包',terms=[])]))
    restored,_=c.transition(excluded,dict(action='undo'))
    assert restored['requirements']==broad
    assert not any(r['mode']=='exclude' for r in widened['requirements'])
    assert widened['scope'] is None and current['scope'] is not None
