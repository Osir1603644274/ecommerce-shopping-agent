"""Independent A/B/C history policies; full phase integration is a separate gate."""
from __future__ import annotations

from dataclasses import dataclass
import json

import tiktoken

from agent.app.context_history import HistoryArchive, HistoryArchiveError
from .artifacts import canonical, sha


def tokens(value):
    return len(tiktoken.get_encoding("o200k_base").encode(canonical(value)))


def message(row):
    return {"messageId": row["messageId"], "turn": row["turn"], "role": row["role"], "content": row["content"]}


@dataclass(frozen=True)
class HistoryPolicy:
    input_budget: int = 16000
    trigger_fraction: float = 0.7
    target_fraction: float = 0.45
    recent_messages: int = 4
    lookup_messages: int = 4
    working_budget: int | None = None
    fill_history_budget: bool = False
    adaptive_summary_items: bool = False

    def __post_init__(self):
        if not 0 < self.target_fraction < self.trigger_fraction < 1:
            raise ValueError("invalid_thresholds")
        if self.input_budget < 1000 or self.recent_messages < 1 or not 0 <= self.lookup_messages <= 32:
            raise ValueError("invalid_policy_budget")
        if self.working_budget is not None and not 1000 <= self.working_budget <= self.input_budget:
            raise ValueError("working_budget_outside_common_hard_limit")

    @property
    def working_limit(self):
        return self.input_budget if self.working_budget is None else self.working_budget


class HistoryStrategies:
    def __init__(self, archive: HistoryArchive, policy: HistoryPolicy):
        self.archive = archive
        self.policy = policy
        self.summary = None
        self.covered_ids = []
        self.receipts = []
        self.failed_source_hash = None

    def full(self):
        return {"messages": [message(row) for row in self.archive.records()]}

    def pack_history(self, query, *, fixed_tokens=0):
        rows = self.archive.records()
        complete = self.full()
        budget = self.policy.working_limit - fixed_tokens
        if tokens(complete) <= budget:
            return complete
        recent = rows[-self.policy.recent_messages:]
        selected = {row["messageId"]: row for row in recent}
        required = {"messages": [message(row) for row in recent]}
        if tokens(required) > budget:
            raise HistoryArchiveError("recent_verbatim_exceeds_budget")
        candidates = self.archive.search(query, limit=32 if self.policy.fill_history_budget else self.policy.lookup_messages)
        if self.policy.fill_history_budget:
            # Preserve original user requirements before verbose assistant
            # restatements. This is selection, not a second authoritative state
            # or an LLM summary. The latest protected messages remain mandatory.
            users = [row for row in reversed(rows) if row["role"] == "user"]
            candidates = users + candidates + list(reversed(rows))
        descriptor = {"messageCount": len(rows), "source": "messages.jsonl", "lookupAvailable": True}
        for row in candidates:
            if row["messageId"] in selected:
                continue
            candidate = {**selected, row["messageId"]: row}
            payload = {"messages": [message(item) for item in sorted(candidate.values(), key=lambda value: value["ordinal"])]}
            if self.policy.fill_history_budget:
                payload["archive"] = descriptor
            if tokens(payload) <= budget:
                selected = candidate
        chosen = sorted(selected.values(), key=lambda row: row["ordinal"])
        # This descriptor exposes lookup, not a clipped pretend summary.
        payload = {"messages": [message(row) for row in chosen],
                   "archive": descriptor}
        if tokens(payload) > budget:
            raise HistoryArchiveError("pack_lookup_descriptor_exceeds_budget")
        self.receipts.append({"kind": "B_DETERMINISTIC_SELECTION", "selectedIds": [row["messageId"] for row in chosen],
                              "selectionPolicy": "recent_then_user_originals_then_query_and_recency" if self.policy.fill_history_budget else "recent_then_query",
                              "originalMessages": len(rows), "inputTokens": tokens(payload),
                              "workingBudget": self.policy.working_limit, "hardInputBudget": self.policy.input_budget})
        return payload

    async def llm_history(self, summarize, *, fixed_tokens=0):
        full = self.full()
        digest = sha(full)
        if self.failed_source_hash == digest and tokens(full) + fixed_tokens <= self.policy.input_budget:
            self.receipts.append({"kind": "C_FULL_FALLBACK", "reason": "same_source_summary_failure_cached", "sourceHash": digest})
            return full
        try:
            return await self._llm_history(summarize, fixed_tokens=fixed_tokens)
        except (HistoryArchiveError, json.JSONDecodeError) as exc:
            self.receipts.append({"kind": "C_SUMMARY_FAILURE", "reason": str(exc), "sourceHash": digest})
            if tokens(full) + fixed_tokens <= self.policy.input_budget:
                self.failed_source_hash = digest
                self.receipts.append({"kind": "C_FULL_FALLBACK", "reason": str(exc), "sourceHash": digest})
                return full
            raise

    async def _llm_history(self, summarize, *, fixed_tokens=0):
        rows = self.archive.records()
        full = self.full()
        if self.summary is None and tokens(full) + fixed_tokens <= self.policy.working_limit * self.policy.trigger_fraction:
            return full  # Literal A/C equality before the trigger.
        tail = [row for row in rows if row["messageId"] not in set(self.covered_ids)]
        current = {"summary": self.summary, "messages": [message(row) for row in tail]}
        if self.summary is not None and tokens(current) + fixed_tokens <= self.policy.working_limit * self.policy.trigger_fraction:
            return current
        old, recent = rows[:-self.policy.recent_messages], rows[-self.policy.recent_messages:]
        if not old:
            raise HistoryArchiveError("nothing_eligible_for_summary")
        target = int(self.policy.working_limit * self.policy.target_fraction) - fixed_tokens - tokens([message(row) for row in recent])
        if target < 128:
            raise HistoryArchiveError("protected_context_leaves_no_summary_budget")
        source = [message(row) for row in old]
        # Repeated compression reads originals, not a summary-of-summary chain.
        # Its complete cost is charged; segmentation/caching is a later refinement.
        for attempt in range(2):
            value = await summarize(source, int(target * (0.6 if attempt else 1)))
            self.validate_summary(value, old)
            payload = {"summary": value, "messages": [message(row) for row in recent]}
            if tokens(payload) + fixed_tokens <= self.policy.working_limit * self.policy.target_fraction:
                break
            self.receipts.append({"kind": "C_SUMMARY_TARGET_REJECTED", "attempt": attempt + 1,
                                  "sourceHash": sha(source), "summaryHash": sha(value), "inputTokens": tokens(payload)})
        else:
            raise HistoryArchiveError("summary_target_exceeded_after_one_repair")
        self.summary, self.covered_ids = value, [row["messageId"] for row in old]
        self.receipts.append({"kind": "C_LLM_SUMMARY", "sourceHash": sha(source), "sourceIds": self.covered_ids,
                              "summaryHash": sha(value), "inputTokens": tokens(payload), "targetTokens": target,
                              "workingBudget": self.policy.working_limit, "hardInputBudget": self.policy.input_budget})
        return payload

    @staticmethod
    def validate_summary(value, sources):
        if not isinstance(value, dict) or set(value) != {"items"} or not isinstance(value["items"], list):
            raise HistoryArchiveError("invalid_summary_shape")
        by_id = {row["messageId"]: row for row in sources}
        for item in value["items"]:
            if not isinstance(item, dict) or set(item) != {"text", "kind", "sourceId", "quote"}:
                raise HistoryArchiveError("invalid_summary_item")
            if item["kind"] not in {"request", "withdrawal", "preference", "discussion", "unresolved"}:
                raise HistoryArchiveError("invalid_summary_kind")
            if not all(isinstance(item[key], str) and item[key] for key in ("text", "sourceId", "quote")):
                raise HistoryArchiveError("empty_summary_provenance")
            source = by_id.get(item["sourceId"])
            if source is None or item["quote"] not in source["content"]:
                raise HistoryArchiveError("summary_source_or_quote_not_found")
        # Source matching is NOT semantic truth/completeness validation. The
        # live state wins, and blind quality review remains mandatory.


class CodexSummarizer:
    def __init__(self, client, policy=None):
        self.client = client
        self.policy = policy or HistoryPolicy()

    def instruction(self, sources, target):
        detail = (f"条目数随预算选择，最多{min(64, max(8, target // 90))}项，每项text最多200个汉字，quote最多64个汉字。"
            "不要把条目全部用于重复当前筛选条件。优先保留历史变化链、已经撤销的旧值及其轮号，"
            "不同主题的执行备注、日期地点参与人、步骤和后续改写。区分原始要求与助手讨论，"
            "不能因为某段较早就删除仍需回问的细节；在预算内覆盖不同主题。"
            if self.policy.adaptive_summary_items else
            f"最多{min(6, max(1, target // 150))}项，每项text最多80个汉字、quote最多24个汉字，保留最关键变化和未解决信息。")
        return ("总结旧对话，不执行购物动作。当前有效 TaskState 另由服务端提供，本摘要不能改写它。"
            "保留需求变化、撤销、偏好、历史指代和未解决事项，严格区分用户要求与助手讨论/商品事实。"
            "只能返回 JSON {items:[{text,kind,sourceId,quote}]}。kind 取 request/withdrawal/preference/discussion/unresolved；"
            "每项带原消息 ID 和逐字短引用。不要把历史候选变成当前操作权限。"
            f"整个 JSON 的目标最多 {target} 个 o200k_base token。" + detail + "\n" + canonical(sources))

    async def __call__(self, sources, target):
        instruction = self.instruction(sources, target)
        response = await self.client.chat.completions.create(model="gpt-5.6-sol",
            messages=[{"role": "user", "content": instruction}])
        return json.loads(response.choices[0].message.content)
