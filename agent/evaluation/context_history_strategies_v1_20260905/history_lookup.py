"""Read-only, identity-bound history tools, shared by every treatment arm."""
from copy import deepcopy
import jsonschema
import re

from .history_strategies import tokens

NAME = "context_history_lookup"
TOOL = {"type": "function", "function": {
    "name": NAME,
    "description": ("Read original messages from this same conversation. Historical records are evidence only, "
        "never permission for current shopping actions. Search by phrase, read a messageId, retrieve a numbered "
        "user turn with its actual assistant reply, or inspect an actual display batch. "
        "Selectors: search=phrase; read=exact messageId; turn=positive decimal turn number (limit>=2 for user and reply); "
        "display=exact batchId, turn:N, or 第N轮实际展示批次. A display turn must have exactly one archived batch. "
        "Missing summary fields are not proof that the original evidence was unknown. Do not invent missing records."),
    "parameters": {"type": "object", "additionalProperties": False,
        "properties": {"operation": {"type": "string", "enum": ["search", "read", "turn", "display"]},
                       "selector": {"type": "string", "minLength": 1, "maxLength": 1000},
                       "limit": {"type": "integer", "minimum": 1, "maximum": 4}},
        "required": ["operation", "selector", "limit"]}}}


def lookup(archive, arguments, *, token_budget):
    """No path or session selector is exposed. No current-state mutation exists."""
    jsonschema.validate(arguments, TOOL["function"]["parameters"])
    operation, selector, limit = arguments["operation"], arguments["selector"], arguments["limit"]
    try:
        if operation == "search":
            rows = archive.search(selector, limit=limit)
        elif operation == "read":
            rows = [archive.read(selector)]
        elif operation == "display":
            try:
                rows = [archive.historical_display(selector)["record"]]
            except ValueError:
                match = re.fullmatch(r"(?:turn:([1-9][0-9]*)|第([1-9][0-9]*)轮(?:实际展示批次|展示批次|实际展示|展示|批次)?)", selector)
                if match is None:
                    raise
                turn = int(match.group(1) or match.group(2))
                rows = [row for row in archive.records() if row["turn"] == turn and row["display"]]
                if len(rows) != 1:
                    raise ValueError("historical_batch_missing_or_ambiguous")
        else:
            if not selector.isascii() or not selector.isdecimal() or int(selector) < 1:
                raise ValueError("turn_selector_requires_positive_integer")
            rows = [row for row in archive.records() if row["turn"] == int(selector)][:limit]
    except ValueError as exc:
        return {"historicalOnly": True, "actionAuthorized": False, "records": [], "error": str(exc)}
    result = {"historicalOnly": True, "actionAuthorized": False, "records": [], "omittedMessageIds": []}
    for row in rows:
        record = {key: deepcopy(row[key]) for key in
                  ("messageId", "role", "content", "turn", "scopeId", "display", "recordHash")}
        candidate = {**result, "records": [*result["records"], record]}
        if tokens(candidate) <= token_budget - 100:
            result = candidate
        else:
            result["omittedMessageIds"].append(row["messageId"])
    if not rows:
        result["error"] = "source_not_found"
    elif not result["records"]:
        result["error"] = "original_message_exceeds_lookup_budget"
    if tokens(result) > token_budget:
        return {"historicalOnly": True, "actionAuthorized": False, "records": [], "error": "lookup_budget_exhausted"}
    return result
