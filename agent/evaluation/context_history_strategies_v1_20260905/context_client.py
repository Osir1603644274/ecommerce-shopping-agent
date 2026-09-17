"""Actual per-model-phase A/B/C context boundary over the shared Agent flow."""
from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace

from agent.app.context_input import BOUNDARY
from agent.app.task_state import get_task_state
from agent.app.domains.ecommerce.shopping_state_authority import select_shopping_state_authority
from agent.app.settings import settings
from .artifacts import append, canonical, sha, write_new
from .history_strategies import CodexSummarizer, tokens
from .history_lookup import TOOL as HISTORY_TOOL, NAME as HISTORY_TOOL_NAME, lookup
from .subscription import decode_answer

ARMS = ("A_FULL_HISTORY", "B_PACK_VIEW", "C_LLM_THRESHOLD")


def business_task_state(state):
    """Full business state, not repeated backend execution/audit envelopes.

    Source is live TaskState, never a Pack. Audit/checkpoint/raw tool receipts
    stay in artifacts. Current validated tool evidence travels in phaseContract.
    """
    value = state.model_dump(by_alias=True, mode="json")
    if state.task_type != "ecommerce_guide":
        raise ValueError("unregistered_business_state_boundary")
    selection = select_shopping_state_authority(domain_state=state.domain_state, task_id=state.task_id,
        task_revision=state.revision, goal=state.goal, unknowns=state.unknowns,
        pending_questions=state.pending_questions, mode=settings.shopping_state_authority)
    # Full V2 business object has no execution receipts. Only an empty initial
    # state may lack it; legacy migration policy remains the server's decision.
    v2 = state.domain_state.get("shoppingTaskStateV2")
    value["domainState"] = {"shoppingTaskStateV2": deepcopy(v2)} if v2 is not None else deepcopy(selection.domain_state)
    return value


def raw_phase_contract(phase, payload):
    """Keep executable bindings and current evidence, not a reconstructed Pack.

    Planner/replanner argument-source contracts remain common in all arms;
    removing them would change allowed actions rather than context strategy.
    """
    if phase == "extraction":
        keys = {"taskId", "baseContextRevision", "allowedTools", "contextPolicyId", "contextPolicyVersion",
                "contextSkillId", "contextSkillVersion"}
    elif phase == "final_answer":
        keys = {"taskId", "baseContextRevision", "phaseTaskRevision", "validatedResults", "evidenceRefs", "answerFormat"}
    else:
        # Decision option IDs/hashes and planner sources are server-owned and
        # must remain literal. They are not independently selected per arm.
        return deepcopy(payload)
    return {key: deepcopy(value) for key, value in payload.items() if key in keys}


class ContextClient:
    def __init__(self, base, *, arm, history, task_id, session_id, query, output, state_loader=get_task_state):
        if arm not in ARMS:
            raise ValueError("unknown_history_arm")
        self.base, self.arm, self.history = base, arm, history
        self.task_id, self.session_id, self.query = task_id, session_id, query
        self.output = output
        self.state_loader = state_loader
        self.chat = SimpleNamespace(completions=self)
        self.receipts = []

    def _record_native_bindings(self, first, purpose, phase, revision):
        for call in getattr(self.base, "calls", [])[first:]:
            append(self.output.parent / "native_call_bindings.jsonl", {
                "nativeOrdinal": call["ordinal"], "nativeRequestSha256": call.get("requestSha256"),
                "phase": phase, "purpose": purpose, "taskId": self.task_id, "stateRevision": revision,
                "status": call["status"], "arm": self.arm})

    async def create(self, **request):
        wire = deepcopy(request)
        business_tools = deepcopy(request.get("tools") or [])
        business_choice = deepcopy(request.get("tool_choice", "auto"))
        wire["tools"] = [*business_tools, deepcopy(HISTORY_TOOL)]
        wire["tool_choice"] = "auto"
        wire["parallel_tool_calls"] = False
        tagged = []
        for ordinal, entry in enumerate(wire["messages"]):
            try:
                value = json.loads(entry.get("content") or "")
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict) and value.get("boundary") == BOUNDARY:
                tagged.append((ordinal, value))
        if len(tagged) != 1:
            raise ValueError("missing_or_multiple_model_context_boundaries")
        ordinal, tagged_value = tagged[0]
        if set(tagged_value) != {"boundary", "phase", "payload"}:
            raise ValueError("invalid_context_boundary")
        phase, payload = tagged_value["phase"], tagged_value["payload"]
        if phase == "final_answer":
            wire["messages"].append({"role": "system", "content":
                "本轮回答边界：先回答currentUserMessage。validatedResults中的productPresentations是Validator已放行的"
                "初筛展示证据，可按其本轮顺序展示已知字段；allowedFacts为空不否定这些独立的工具证据。"
                "尚未调用详情或比较工具时，不能宣称已经完成这些步骤或证明全局最优，但不能因此把已有初筛候选说成不存在。"
                "最近展示批次以本轮验证证据及实际展示记录为准，不把历史商品混入当前列表。"
                "answerFormat.currentTurnExecution只记录当前run实际成功执行的工具顺序，不包含旧轮工具；"
                "若其中依次有search_products和compare_products，表示本轮已经重搜并比较，不能因validatedResults只保留末步比较而说没重搜。"
                "工具执行记录不补造商品属性，商品事实仍必须引用validatedResults；comparisonSelection绑定当前验证范围的原始序号，不能一概称作旧轮结果。"
                "遵守answerFormat的展示上限；超出时说明这是本轮展示上限而非候选总数。"
                "用户仅要求备注或历史回顾时，不用无关商品列表替代回答；历史记录不授予当前购物操作权限。"})
        state = await self.state_loader(self.task_id)
        if state is None or state.task_id != self.task_id or state.session_id != self.session_id:
            raise ValueError("context_state_identity_mismatch")
        declared_task = payload.get("taskId", payload.get("context", {}).get("taskId"))
        if declared_task and declared_task != self.task_id:
            raise ValueError("phase_task_identity_mismatch")
        if self.arm == "B_PACK_VIEW":
            fixed = {"phaseContext": payload, "currentUserMessage": self.query}
        else:
            fixed = {"taskState": business_task_state(state),
                     "phaseContract": raw_phase_contract(phase, payload), "currentUserMessage": self.query}
        wire["messages"][ordinal]["content"] = canonical(fixed)
        fixed_tokens = tokens(wire)
        receipt_offset = len(self.history.receipts)
        native_offset = len(getattr(self.base, "calls", []))
        try:
            if self.arm == "A_FULL_HISTORY":
                selected = self.history.full()
            elif self.arm == "B_PACK_VIEW":
                selected = self.history.pack_history(self.query, fixed_tokens=fixed_tokens)
            else:
                selected = await self.history.llm_history(CodexSummarizer(self.base, self.history.policy), fixed_tokens=fixed_tokens)
        finally:
            self._record_native_bindings(native_offset, "history_summary", phase, state.revision)
            for history_receipt in self.history.receipts[receipt_offset:]:
                append(self.output.parent / "history_receipts.jsonl", history_receipt)
        rendered = {**fixed, "history": selected}
        wire["messages"][ordinal]["content"] = canonical(rendered)
        # Request JSON is an application-level diagnostic, not the CLI's hidden
        # provider wire. A above-window run is held rather than truncated.
        input_tokens = tokens(wire)
        if input_tokens > self.history.policy.input_budget:
            # Preserve the exact rejected application request, not only an
            # exception string. No native model call has started for this wire.
            rejection_number = len(list(self.output.parent.glob("preflight-rejected-*.json"))) + 1
            write_new(self.output.parent / f"preflight-rejected-{rejection_number:03d}.json", {
                "arm": self.arm, "phase": phase, "taskId": state.task_id, "revision": state.revision,
                "modelCalled": False, "applicationRequestTokens": input_tokens,
                "inputBudget": self.history.policy.input_budget, "requestSha256": sha(wire), "request": wire,
                "reason": "application_input_budget_exceeded"})
            raise ValueError("application_input_budget_exceeded:" + str(input_tokens))
        receipt = {"arm": self.arm, "phase": phase, "taskId": state.task_id, "revision": state.revision,
            "sourceStateHash": sha(state.model_dump(mode="json", by_alias=True)),
            "originalPhasePayloadHash": sha(payload), "selectedHistoryHash": sha(selected),
            "renderedRequestHash": sha(wire), "applicationRequestTokens": input_tokens,
            "answerFormat": deepcopy(payload.get("answerFormat")) if phase == "final_answer" else None}
        self.receipts.append(receipt)
        append(self.output, receipt)
        for lookup_round in range(3):
            if lookup_round == 2:
                wire["tools"] = business_tools
                wire["tool_choice"] = business_choice
            count = tokens(wire)
            if count > self.history.policy.input_budget:
                raise ValueError("lookup_application_input_budget_exceeded:" + str(count))
            native_offset = len(getattr(self.base, "calls", []))
            try:
                response = await self.base.chat.completions.create(**wire)
            finally:
                self._record_native_bindings(native_offset, "agent_phase" if lookup_round == 0 else "agent_phase_after_lookup", phase, state.revision)
            response_message = response.choices[0].message
            calls = response_message.tool_calls or []
            reads = [call for call in calls if call.function.name == HISTORY_TOOL_NAME]
            if not reads:
                # Restore the original business contract, including named tool
                # choice, before handing anything back to the real Agent.
                decode_answer(canonical({"content": response_message.content, "toolCalls": [
                    {"name": call.function.name, "arguments": call.function.arguments} for call in calls]}), request)
                return response
            if len(reads) != 1 or len(calls) != 1 or lookup_round == 2:
                raise ValueError("mixed_or_exhausted_history_lookup")
            arguments = json.loads(reads[0].function.arguments)
            source_result = lookup(self.history.archive, arguments,
                token_budget=max(128, min(2000, self.history.policy.input_budget - count - 500)))
            append(self.output.parent / "history_lookups.jsonl", {"phase": phase,
                "lookupRound": lookup_round + 1, "arguments": arguments, "result": source_result,
                "requestHash": sha(wire), "applicationRequestTokens": count})
            wire["messages"].append({"role": "assistant", "content": response_message.content,
                "tool_calls": [{"id": reads[0].id, "type": "function", "function": {
                    "name": HISTORY_TOOL_NAME, "arguments": reads[0].function.arguments}}]})
            wire["messages"].append({"role": "tool", "tool_call_id": reads[0].id, "content": canonical(source_result)})
        raise ValueError("history_lookup_loop_not_closed")
