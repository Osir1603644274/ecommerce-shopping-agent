"""Shared direct-chat execution of the validated guide decision.

The workspace keeps its checkpointed presentation workflow; this adapter uses
the same interpreter, canonical TaskState and catalog service for JSON/SSE.
Transactions remain in the existing durable business runner.
"""
from __future__ import annotations

import time
from typing import Any

from .catalog_service import fingerprint
from .guide_execution import answer as answer_catalog, retrieve as retrieve_catalog, select_provider_query
from .guide_evidence import publish_scope, read_scope
from .guide_interpreter import interpret_and_commit
from .guide_state import ShoppingState, catalog_projection
from .schemas import ToolTrace
from .task_state import TaskState


async def _presentation(task: TaskState, session_id: str | None) -> dict[str, Any]:
    guide = ShoppingState.model_validate(task.domain_state["shopping"])
    scope = await read_scope(task)
    if scope is not None and scope.get("query") != guide.query:
        raise ValueError("guide evidence query changed")
    cards: list[dict[str, Any]] = []
    if scope is not None and session_id:
        from .catalog_commerce import resolve_optional_cards

        resolved, _ = await resolve_optional_cards(scope)
        by_docid = {card.get("sourceDocid"): card for card in resolved}
        for group in scope.get("groups", []):
            card = next((by_docid[m["docid"]] for m in group["members"]
                         if m["docid"] in by_docid), None)
            cards.append(card or {"title": group["title"]})
    return {"engine": session_id or "", "catalogSearch": catalog_projection(guide, scope=scope),
            "cards": cards, "messages": []}


async def execute_guide_turn(
    message: str, task: TaskState, *, session_id: str | None,
    turn_id: str, on_answer_delta: Any = None, on_task_state: Any = None,
) -> tuple[TaskState, tuple[str, list[ToolTrace], list[dict], str | None, None] | None]:
    """Return None for business; only bounded read-only guide actions execute here."""
    from .guide_state import ensure_shopping_state

    task = await ensure_shopping_state(task)
    view = await _presentation(task, session_id)
    task, plan, receipt = await interpret_and_commit(message, task, workspace=view, turn_id=turn_id)
    if on_task_state is not None:
        await on_task_state(task, "guide_interpreted")
    if plan["route"] == "business":
        return task, None
    trace: list[ToolTrace] = []
    current = catalog_projection(ShoppingState.model_validate(task.domain_state["shopping"]))
    current["scope"] = view["catalogSearch"].get("scope") if not task.domain_state["guideTurnDecision"]["semanticChanged"] else None
    if plan["route"] == "product":
        from .product_followup import answer_question, read_facts

        facts = await read_facts(plan)
        answer, answer_receipt = await answer_question(message, plan, facts)
        trace.append(ToolTrace(tool="read_product_facts", ok=True,
                               detail={"modelCall": answer_receipt, "factCount": len(facts)}))
    else:
        action = plan["action"]
        no_undo = action == "undo" and not task.domain_state["guideTurnDecision"]["semanticChanged"]
        if action in {"search", "refine", "new", "undo"} and current["query"] and not no_undo:
            began = time.perf_counter()
            provider_query, provider_reason = select_provider_query(current)
            current["scope"] = await retrieve_catalog(
                current["query"], provider_query, current["requirements"])
            trace.append(ToolTrace(tool="search_catalog", ok=True,
                                   durationMs=(time.perf_counter() - began) * 1000,
                                   detail={"scopeId": current["scope"]["scopeId"],
                                           "providerQuery": provider_query,
                                           "queryReason": provider_reason,
                                           "stateQueryReason": (receipt.get("queryDecision") or {}).get("reason")}))
        if no_undo:
            answer, answer_receipt = "没有可以撤销的需求修改。", None
        elif action == "undo" and not current["query"]:
            answer, answer_receipt = "已撤销上次需求修改，当前没有正在检索的商品需求。", None
        else:
            answer, answer_receipt, current["scope"] = await answer_catalog(message, plan, current)
        review = (answer_receipt or {}).get("scopeReview")
        if current.get("scope") and not no_undo and (action in {"search", "refine", "new", "undo"} or review):
            task = await publish_scope(task, current["scope"], run_id=turn_id)
            if on_task_state is not None:
                await on_task_state(task, "guide_evidence_published")
        trace.append(ToolTrace(tool="answer_catalog", ok=True,
                               detail={"modelCall": answer_receipt, "evidenceSha256": fingerprint(current.get("scope"))}))
    if on_answer_delta is not None:
        await on_answer_delta(answer)
    return task, (answer, trace, [{"role": "user", "content": message},
                                  {"role": "assistant", "content": answer}], turn_id, None)
