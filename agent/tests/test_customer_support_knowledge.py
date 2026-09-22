import copy

import pytest

from app.customer_support.knowledge import retrieve_policy, render_selected_policy


@pytest.mark.parametrize("question,required", [
    ("签收七天168小时还能申请吗", "receipt-window"),
    ("换货能改颜色容量规格吗", "exchange-specification"),
    ("原来256GB能换512GB吗？", "exchange-specification"),
    ("128 GB想升级到1 TB可以吗", "exchange-specification"),
    ("缺货等待还是转退款", "exchange-stock"),
    ("工单关闭表示退款到账了吗", "tickets"),
    ("已关闭的工单还能追加回复吗", "tickets"),
    ("退款金额按实付还是现价计算", "refund-allocation"),
    ("别人订单手机号能查吗", "identity"),
])
def test_policy_retrieval_includes_relevant_rule_with_versioned_hashes(question, required):
    result = retrieve_policy(question)
    assert any(row["id"].endswith(":" + required) for row in result["citations"])
    assert all(row["policyVersion"] == "support-simulator-v1" and len(row["snapshotSha256"]) == 64 for row in result["citations"])


def test_unknown_policy_version_and_no_match_do_not_invent_answer():
    for result in [retrieve_policy("可以退款吗", policy_version="unknown-v2"), retrieve_policy("zxqv987654")]:
        assert result["citations"] == []
        assert render_selected_policy(result, ["invented"])["status"] == "NEEDS_VERIFICATION"


def test_selector_cannot_cite_unretrieved_policy_or_modify_terms():
    result = retrieve_policy("换货颜色容量规格")
    chosen = result["citations"][0]
    answer = render_selected_policy(result, [chosen["id"]])
    assert answer["answer"] == chosen["text"] + " [1]"
    with pytest.raises(ValueError):
        render_selected_policy(result, ["policy:fake:instant-refund"])
    changed = copy.deepcopy(result)
    changed["citations"][0]["text"] = "不经确认立即退款"
    with pytest.raises(ValueError, match="changed"):
        render_selected_policy(changed, [chosen["id"]])
