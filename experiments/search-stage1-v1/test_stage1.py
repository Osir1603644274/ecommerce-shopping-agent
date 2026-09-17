from __future__ import annotations

import itertools
import json
from pathlib import Path
import sys

import numpy as np
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parent))
from data_prep import NearIndex
from metrics import dcg, query_metrics, paired_bootstrap
from retrieve import stable_topk, frozen_jsonl
from silver import validate_judgments, RUBRIC
from silver import anonymous,reconcile,adjudication,finalize,merge
from stage1 import dump, digest, key


def test_query_family_exact_and_near():
    near=NearIndex()
    near.add(key("ＡBC　手机！"))
    assert near.match(key("abc手机"))
    near.add("儿童夏季透气运动防滑网面跑步鞋")
    assert near.match("儿童夏季透气运动防滑网面跑步鞋子")
    assert not near.match("儿童冬季保暖棉靴")


def test_streaming_topk_matches_full_sort_with_boundary_ties():
    scores=np.array([.9,.4,.9,.8,.8,.8,.1],dtype=np.float32)
    ids=np.array([7,6,5,4,3,2,1])
    best_s,best_i=np.empty(0,dtype=np.float32),np.empty(0,dtype=np.int64)
    for offset in range(0,len(scores),2):
        best_s,best_i=stable_topk(np.r_[best_s,scores[offset:offset+2]],np.r_[best_i,ids[offset:offset+2]],4)
    expected=np.lexsort((ids,-scores))[:4]
    np.testing.assert_array_equal(best_i,ids[expected])
    np.testing.assert_array_equal(best_s,scores[expected])


def test_unknown_ndcg_bounds_cover_every_possible_assignment():
    rankings=[list("abcde"),list("edcba"),list("caebd")]
    qrels={"a":3,"b":1,"c":"UNKNOWN","d":0,"e":"UNKNOWN"}
    for ranking in rankings:
        result=query_metrics(ranking,qrels)
        assert result["ndcg_at_10"] is None
        for c,e in itertools.product(range(4),repeat=2):
            concrete={**qrels,"c":c,"e":e}
            exact=dcg([concrete[d] for d in ranking])/dcg(sorted(concrete.values(),reverse=True))
            assert result["ndcg_lower_bound_at_10"] <= exact+1e-12
            assert exact <= result["ndcg_upper_bound_at_10"]+1e-12
    assert qrels["c"]=="UNKNOWN"


def test_unknown_is_not_judged_zero_or_outside_pool_negative():
    r=query_metrics(["positive","unknown","outside"],{"positive":3,"unknown":"UNKNOWN","zero":0})
    assert r["judged_at_10"]==.1
    assert r["unknown_top10"]==1 and r["outside_pool_top10"]==1
    assert r["known_relevant_recall_at_10"]==1
    assert query_metrics(["a"],{"a":0})["ndcg_at_10"] is None


def test_paired_bootstrap_direction_and_missing():
    result=paired_bootstrap({"q":.4,"empty":None},{"q":.6,"empty":.3},repetitions=10)
    assert result["queries"]==1
    assert result["delta"]==pytest.approx(.2)


def test_frozen_artifact_refuses_change(tmp_path):
    path=tmp_path/"frozen.jsonl"
    frozen_jsonl(path,[{"a":1}])
    frozen_jsonl(path,[{"a":1}])
    with pytest.raises(ValueError): frozen_jsonl(path,[{"a":2}])


def test_judgment_validation_preserves_unknown_and_detects_missing_duplicate(tmp_path):
    frozen_jsonl(tmp_path/"packet.jsonl",[{"query_id":"q","candidates":[{"document_id":"a"},{"document_id":"b"}]}])
    (tmp_path/"RUBRIC.md").write_text(RUBRIC,encoding="utf-8")
    dump(tmp_path/"manifest.json",{"packet_sha256":digest(tmp_path/"packet.jsonl"),"rubric_sha256":digest(tmp_path/"RUBRIC.md")})
    j={"query_id":"q","document_id":"a","grade":"UNKNOWN","reason":"缺少关键规格信息"}
    frozen_jsonl(tmp_path/"judgments.jsonl",[j])
    actual,missing=validate_judgments(tmp_path)
    assert actual[("q","a")]["grade"]=="UNKNOWN" and missing=={("q","b")}
    with (tmp_path/"judgments.jsonl").open("a",encoding="utf-8") as f: f.write(json.dumps(j)+"\n")
    with pytest.raises(ValueError): validate_judgments(tmp_path)


def test_two_judges_third_vote_and_unknown_survive_final_export(tmp_path):
    qid=anonymous("q","q1")
    dids=[anonymous("d",f"d{i}") for i in range(4)]
    mapping=[{"query_id":qid,"document_id":did,"original_query_id":"q1","original_document_id":f"d{i}"} for i,did in enumerate(dids)]
    frozen_jsonl(tmp_path/"labeling/provenance/fixture-dev.mapping.jsonl",mapping)
    receipts=[]
    for role,grades in (("primary",[3,"UNKNOWN",2,0]),("review",[3,"UNKNOWN",1,2])):
        target=tmp_path/"labeling/packets"/role
        frozen_jsonl(target/"packet.jsonl",[{"query_id":qid,"candidates":[{"document_id":d} for d in dids]}])
        (target/"RUBRIC.md").write_text(RUBRIC,encoding="utf-8")
        dump(target/"manifest.json",{"packet_sha256":digest(target/"packet.jsonl"),"rubric_sha256":digest(target/"RUBRIC.md")})
        frozen_jsonl(target/"judgments.jsonl",[{"query_id":qid,"document_id":d,"grade":g,"reason":"仅用于单元测试的判据"} for d,g in zip(dids,grades)])
        receipts.append({"role":role,"directory":str(target)})
    dump(tmp_path/"labeling/provenance/fixture-dev.packets.json",{"packets":receipts})
    reconcile(tmp_path,"fixture","dev")
    frozen_jsonl(tmp_path/"pools/inputs/fixture-dev/documents.jsonl",[{"document_id":f"d{i}","title":f"测试商品{i}"} for i in range(4)])
    frozen_jsonl(tmp_path/"queries.selected.jsonl",[{"query_id":"q1","text":"测试商品"}])
    adjudication(tmp_path,"fixture","dev")
    packet=json.loads((tmp_path/"labeling/reconciliation/fixture-dev/adjudication-packets.json").read_text(encoding="utf-8-sig"))["packets"][0]
    target=Path(packet["directory"])
    frozen_jsonl(target/"judgments.jsonl",[{"query_id":qid,"document_id":dids[2],"grade":"UNKNOWN","reason":"第三人仍缺少证据"},
                                        {"query_id":qid,"document_id":dids[3],"grade":2,"reason":"与第二人同一判断"}])
    with pytest.raises(ValueError,match="author collection"):
        finalize(tmp_path,"fixture","dev")
    assert not (tmp_path/"qrels/base-v1/fixture-dev/manifest.json").exists()
    authors=[]
    for p in [Path(r["directory"]) for r in receipts]+[target]:
        dump(p/"completion.json",{"fixture_only":True})
        authors.append({"thread_id":p.name,"packet":p.name,"judge_model_metadata":{"model":"unit-test-fixture","source_thread_id":p.name},
                        "judgments_sha256":digest(p/"judgments.jsonl"),"completion_sha256":digest(p/"completion.json")})
    dump(tmp_path/"labeling/collection.json",{"imported":authors})
    finalize(tmp_path,"fixture","dev")
    frozen_jsonl(tmp_path/"evaluation/topup/fixture-dev/required.jsonl",[])
    merge(tmp_path,"fixture","dev")
    final=tmp_path/"qrels/final-v1/fixture-dev"
    values=[json.loads(line) for line in (final/"qrels.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["grade"] for r in values]==[3,"UNKNOWN","UNKNOWN",2]
    assert len((final/"qrels.tsv").read_text(encoding="utf-8-sig").splitlines())==2
    from integrity import verify_qrels
    verify_qrels(final)
    (target/"judgments.jsonl").write_text("changed",encoding="utf-8")
    with pytest.raises(ValueError,match="hash drift"):verify_qrels(final)


def test_dense_last_batch_recovers_manifest_and_rejects_tamper(tmp_path):
    from integrity import seal_dense
    np.save(tmp_path/"embeddings.npy",np.array([[1,0],[0,1]],dtype=np.float16))
    state={"binding":{"documents":2,"dimension":2},"done":2,"complete":True}
    receipt=seal_dense(tmp_path,state)
    assert receipt["complete"] and (tmp_path/"dense.manifest.json").exists()
    np.save(tmp_path/"embeddings.npy",np.array([[0,1],[1,0]],dtype=np.float16))
    with pytest.raises(ValueError,match="hash drift"):seal_dense(tmp_path,state)


def test_model_and_checkpoint_sidecar_drift_rejected(tmp_path):
    from integrity import verify_model,verify_checkpoint
    for name in ("config.json","model.safetensors","adapter_model.safetensors","optimizer.pt","tokenizer.json"):
        (tmp_path/name).write_text("original",encoding="utf-8")
    info={"path":str(tmp_path),"files":[{"name":n,"bytes":8,"sha256":digest(tmp_path/n)} for n in ("config.json","model.safetensors")]}
    verify_model(info)
    dump(tmp_path/"complete.json",{"model_sha256":digest(tmp_path/"adapter_model.safetensors"),"optimizer_sha256":digest(tmp_path/"optimizer.pt")})
    dump(tmp_path/"inference-state.json",{"training_complete_sha256":digest(tmp_path/"complete.json"),"files":[{"name":"tokenizer.json","bytes":8,"sha256":digest(tmp_path/"tokenizer.json")}]})
    verify_checkpoint(tmp_path)
    (tmp_path/"tokenizer.json").write_text("modified",encoding="utf-8")
    with pytest.raises(ValueError,match="hash drift"):verify_checkpoint(tmp_path)
    (tmp_path/"model.safetensors").write_text("modified",encoding="utf-8")
    with pytest.raises(ValueError,match="hash drift"):verify_model(info)


@pytest.mark.parametrize("field",["per_query","mean","ci"])
def test_final_report_rejects_changed_metrics_even_with_valid_pair_coverage(field):
    from report_validation import summaries,validate_report
    ranks={"base_ce":{"q":["a","b"]},"trained_ce":{"q":["b","a"]}}
    labels={"q":{"a":3,"b":0}}
    records=[{"method":m,"query_id":"q",**query_metrics(r["q"],labels["q"])} for m,r in ranks.items()]
    means,comparison=summaries(records,ranks)
    report={"means":means,"paired_before_after":comparison}
    validate_report(report,records,ranks,labels)
    if field=="per_query":records[0]["ndcg_lower_bound_at_10"]+=.01
    if field=="mean":report["means"]["trained_ce"]["ndcg_lower_bound_at_10"]["mean"]+=.01
    if field=="ci":report["paired_before_after"]["ndcg_lower_bound_at_10"]["ci95"][0]+=.01
    with pytest.raises(ValueError):validate_report(report,records,ranks,labels)
