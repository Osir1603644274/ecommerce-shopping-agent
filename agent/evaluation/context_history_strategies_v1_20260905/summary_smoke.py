"""Live C-threshold mechanism check; synthetic development fixture only."""
from __future__ import annotations

import asyncio
from pathlib import Path

from agent.app.context_history import HistoryArchive
from .artifacts import canonical, write_new
from .history_strategies import CodexSummarizer, HistoryPolicy, HistoryStrategies, tokens
from .subscription import SubscriptionClient


async def run(output):
    output.mkdir(parents=True, exist_ok=False)
    archive = HistoryArchive(output / "archive", session_id="summary-smoke", task_id="phone-task", create=True)
    archive.append("user", "给父亲买二手手机，初步预算1500元，华为只是偏好。", turn=1)
    strategy = HistoryStrategies(archive, HistoryPolicy())
    async def forbidden(*args):
        raise AssertionError("compression_before_threshold")
    assert await strategy.llm_history(forbidden) == strategy.full()
    fixture = [
        ("assistant", "收到。后续区分预算硬限制、品牌偏好、已核验机况和未知能力。目录是历史公开快照，模拟价格只供预算排序，不能承诺实际报价、库存、配送或售后。这里尚未展示商品，后续的第一款第二款只能绑定真正展示的列表。"),
        ("user", "父亲主要微信视频、读新闻、导航，不玩游戏。他从来没有用过苹果，所以倾向安卓，但我暂时不把系统设成硬条件。不要把卖家标题里的大电池、高像素当成已经测过的续航或拍照效果。收货备注先记为东门联系家人。"),
        ("assistant", "用途作为当前任务偏好保存；安卓暂时是倾向而不是过滤规则。比较时，价格和目录内已验证机况可以引用，真实续航、视频清晰度与导航稳定性尚未知。东门联系家人是本任务备注，不进入永久用户画像。"),
        ("user", "我刚和家里商量了，现在预算可以提高到1800元以内，1500不再是上限。品牌还是倾向华为，若同预算其他安卓机机况更明确，也可以一起比较。请记住这次是给父亲买，不是按我自己的游戏需求推荐。"),
        ("assistant", "预算更新为1800元，旧1500上限已撤销。华为保持偏好；其他品牌不因此被剔除。购买对象仍是父亲，本次用途仍以微信视频、新闻和导航为主，不能因为商品标题出现游戏就把游戏作为主需求。"),
        ("user", "屏幕和主板方面，我希望先看有来源依据的记录。主板如果明确修过就不要；屏幕是否原装目前只作为比较信息，未知不能算作原装。电池健康不要求一定达到95%，不要替我发明这个门槛。"),
        ("assistant", "区分已知违反和未知：明确主板维修是排除条件；未提供维修记录不能说已经满足。原装屏和电池健康都按已有字段陈述，不额外增加95%电池阈值，也不从名称反推出机况。"),
        ("user", "收货备注有变化，东门那条取消，最终写西门先电话联系。这只影响提醒内容，不应该拿去检索手机或改变商品排序。另外请把每次候选列表实际展示顺序留下来，以后比较时我会说第几款。"),
        ("assistant", "旧东门备注已撤销，新备注为西门先电话联系。备注属于当前任务，不是商品需求。展示批次和顺序要以实际发布内容为准，过去的批次可回查，但不能把过期候选当成当前操作授权。"),
        ("user", "现在请整理已确认的需求和撤销事项。特别不要复活1500元旧上限，也不要恢复东门备注。主板维修限制仍然保留，屏幕未知仍然是未知。最后回答要短，先讲预算和硬条件，再讲偏好与需要核实的地方。"),
        ("assistant", "整理顺序为：当前预算1800元；明确主板维修要排除；华为与安卓是倾向，不扩大为额外硬限制；父亲微信视频、新闻和导航用途；西门先电话联系；对未提供的屏幕、电池及真实能力保持未知。"),
    ]
    for index, (role, text) in enumerate(fixture, 2):
        archive.append(role, text, turn=(index + 1) // 2)
    policy = HistoryPolicy(input_budget=max(1400, int(tokens(strategy.full()) * 1.1)),
                           trigger_fraction=0.7, target_fraction=0.6, recent_messages=2)
    strategy = HistoryStrategies(archive, policy)
    client = SubscriptionClient(output / "model_calls", max_calls=2)
    write_new(output / "fixture.json", {"provenance": "AI_AUTHORED_MECHANISM_TEST_NOT_BENCHMARK",
        "inputTokens": tokens(strategy.full()), "policy": vars(policy), "beforeTriggerEquality": True})
    try:
        compact = await strategy.llm_history(CodexSummarizer(client))
        if not any(row["kind"] == "C_LLM_SUMMARY" for row in strategy.receipts):
            raise AssertionError("fallback_is_not_a_successful_compaction")
        write_new(output / "result.json", {"status": "SUMMARY_MECHANISM_PASS", "context": compact,
            "receipts": strategy.receipts, "nativeCalls": client.calls,
            "sourceQuotesValidated": True, "semanticQuality": "NOT_INDEPENDENTLY_JUDGED",
            "fullAgentIntegration": False})
        print(canonical({"status": "SUMMARY_MECHANISM_PASS", "compactions": len(strategy.receipts),
                         "outputContextTokens": tokens(compact), "modelCalls": len(client.calls)}), flush=True)
    except Exception as exc:
        write_new(output / "result.json", {"status": "SUMMARY_MECHANISM_HOLD", "error": str(exc), "nativeCalls": client.calls})
        raise


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    asyncio.run(run(parser.parse_args().output))
