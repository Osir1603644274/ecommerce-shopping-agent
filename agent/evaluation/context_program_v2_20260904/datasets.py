"""Synthetic scenarios and independent, explicit requirement assertions."""
from copy import deepcopy
from datetime import datetime, timezone

from .common import sha


def requirement(key, operator, value, unit):
    return {"key": key, "operator": operator, "value": value,
            "unit": unit, "priority": "hard", "source": "user"}


def synthetic_state(case_id="synthetic"):
    from agent.app.task_state import TaskState
    instant = datetime(2026, 9, 4, tzinfo=timezone.utc)
    return TaskState(taskId="task-" + case_id, taskType="ecommerce_guide",
        sessionId="session-" + case_id, revision=3, status="ready",
        goal="预算2200以内，只看iOS手机", unknowns=[], pendingQuestions=[],
        domainState={"shoppingGuide": {
            "mode": "recommend", "category": "phone", "useCases": [],
            "requirements": [requirement("os", "eq", "ios", "enum"),
                             requirement("price_minor", "lte", 220000, "CNY_MINOR")],
            "candidateIds": [], "comparedIds": [], "evidenceStatus": "missing"}},
        createdAt=instant, updatedAt=instant)


def extraction_cases():
    cases = []
    def add(family, message, expect, absent=()):
        cases.append({"caseId": f"extract-{len(cases)+1:02d}", "familyId": family,
            "message": message, "expectedHard": expect, "absentKeys": list(absent),
            "provenance": "SYNTHETIC_RULE_AUTHORED_NOT_HUMAN_HOLDOUT"})
    for amount in (1000, 1400, 1600, 1800, 2000, 2500):
        add("budget_override", f"预算改为{amount}元，其他硬条件不变。",
            {"os": "ios", "price_minor": amount * 100})
    for use in ("日常稳定性", "给长辈用是否省心", "微信和视频使用", "长期使用体验", "续航体验", "日常流畅性"):
        add("preserve_hard", f"前面那些硬条件都别放宽，再把{use}也考虑进去。",
            {"os": "ios", "price_minor": 220000})
    for message in ("系统改成安卓，预算不变。", "不看iOS了，改为只看安卓，预算仍是2200元。",
                    "把系统条件换为Android，其他条件不变。", "只看安卓手机，预算还是2200以内。"):
        add("os_override", message, {"os": "android", "price_minor": 220000})
    for message in ("取消预算限制，只看iOS的要求保留。", "价格不设上限了，系统仍只看iOS。",
                    "预算不限，其他条件不变。", "把预算条件撤回，仍然只选iOS手机。"):
        add("budget_withdrawal", message, {"os": "ios"}, ("price_minor",))
    for storage in (128, 256, 512, 1024):
        add("storage_add", f"再加一条硬要求：存储至少{storage}GB，预算和系统条件不变。",
            {"os": "ios", "price_minor": 220000, "storage_gb": storage})
    assert len(cases) == 24
    return cases


def conversations(split, count):
    """Disjoint operation-order families across splits, not human holdout.

    Within each split, amount variants share a family; effective N is reported
    by family, not inflated to the number of generated conversations.
    """
    orders = (("budget", "storage", "preserve"), ("storage", "budget", "preserve")) if split == "dev" else (
        ("preserve", "storage", "budget"), ("storage", "preserve", "budget"),
        ("budget", "preserve", "storage"), ("preserve", "budget", "storage"))
    result = []
    for index in range(count):
        amount = (1500 if split == "dev" else 1700) + (index // len(orders)) * 50
        storage = 128 if index % 2 == 0 else 256
        order = orders[index % len(orders)]
        cid = f"ctxv2-{split}-{index+1:03d}"
        texts = ["想买二手手机，只看iOS，预算2200元以内。"]
        edits = {"budget": f"预算改成{amount}元，其他条件不变。",
                 "storage": f"存储至少{storage}GB，其他硬条件不变。",
                 "preserve": "前面那些硬条件都别放宽，再把日常稳定性也考虑进去。"}
        texts += [edits[name] for name in order]
        texts.append("综合前面全部要求，解释这些条件之间如何取舍，不要擅自放宽条件。")
        expected = {"os": "ios", "price_minor": 220000}
        turns = []
        for t, message in enumerate(texts, 1):
            if t in (2, 3, 4):
                op = order[t-2]
                if op == "budget": expected["price_minor"] = amount * 100
                if op == "storage": expected["storage_gb"] = storage
            turns.append({"turnId": f"{cid}-t{t:02d}", "semanticTurn": t,
                "rawUserText": message, "messageSha256": sha(message),
                "turnProvenance": "SYNTHETIC_SCRIPTED", "expectedHard": deepcopy(expected)})
        result.append({"conversationId": cid, "split": split,
            "familyId": split + ":" + "-".join(order), "turns": turns,
            "datasetRole": "SYNTHETIC_NOT_HUMAN_HOLDOUT"})
    return result


def check_requirements(guide, case):
    """Independent oracle: do not call SUT parser/compiler to derive truth."""
    reqs = guide.get("requirements", [])
    errors = []
    expected_ops = {"os": "eq", "price_minor": "lte", "storage_gb": "gte"}
    for key, expected in case["expectedHard"].items():
        matches = [r for r in reqs if r.get("key") == key]
        if len(matches) != 1 or matches[0].get("value") != expected or matches[0].get("priority") != "hard" or matches[0].get("operator") != expected_ops[key]:
            errors.append("hard_requirement_mismatch:" + key)
    for key in case.get("absentKeys", []):
        if any(r.get("key") == key for r in reqs):
            errors.append("withdrawn_requirement_present:" + key)
    return errors
