"""Opt-in DeepSeek strict wire schema, preserving sparse patch semantics.

Only optional *properties* use null as omission. Required values keep their
meaning, and [] remains an explicit empty array. Strict extraction intentionally
supports scalar/null/string-list values, not arbitrary nested fact documents.
The native contract and all business/permission validation remain authoritative.
"""

from copy import deepcopy
from typing import Any
from urllib.parse import urlparse

from jsonschema import Draft202012Validator


def require_strict_endpoint(base_url: str, model: str) -> None:
    endpoint = urlparse(base_url)
    if (
        endpoint.scheme != "https" or endpoint.hostname != "api.deepseek.com"
        or endpoint.path.rstrip("/") != "/beta"
        or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
        or endpoint.port not in (None, 443)
        or not model.strip().lower().startswith("deepseek-")
    ):
        raise ValueError("strict_task_state_extraction_requires_official_deepseek_beta")


def _strict_schema(native: dict[str, Any]) -> dict[str, Any]:
    if not native:
        # {} is not accepted by the provider's strict-schema compiler. This
        # bounded value vocabulary covers shopping rules and scalar facts.
        return {"anyOf": [
            {"type": "string"}, {"type": "number"}, {"type": "boolean"},
            {"type": "null"}, {"type": "array", "items": {"type": "string"}},
        ]}
    result = deepcopy(native)
    if native.get("type") == "object":
        properties = native["properties"]
        required = set(native.get("required", []))
        result["properties"] = {
            key: _strict_schema(child) if key in required else {
                "anyOf": [_strict_schema(child), {"type": "null"}],
                "description": (child.get("description", "") + " "
                    'JSON null (without quotes) means unchanged; never the string "null". '
                    '[] is an explicit empty array.'),
            }
            for key, child in properties.items()
        }
        result["required"] = list(properties)
        result["additionalProperties"] = False
    elif native.get("type") == "array":
        result["items"] = _strict_schema(native["items"])
    return result


def strict_task_state_tool(native_tool: dict[str, Any]) -> dict[str, Any]:
    tool = deepcopy(native_tool)
    tool["function"]["strict"] = True
    tool["function"]["parameters"] = _strict_schema(native_tool["function"]["parameters"])
    return tool


def decode_strict_task_patch(payload: dict[str, Any], native_tool: dict[str, Any]) -> dict[str, Any]:
    schema = strict_task_state_tool(native_tool)["function"]["parameters"]
    error = next(Draft202012Validator(schema).iter_errors(payload), None)
    if error is not None:
        # Never echo the payload or a jsonschema error (which includes values).
        raise ValueError("strict_task_state_wire_schema_mismatch")

    def decode(value: Any, native: dict[str, Any]) -> Any:
        if native.get("type") == "object":
            required = set(native.get("required", []))
            return {
                key: decode(child, native["properties"][key])
                for key, child in value.items()
                if key in required or child is not None
            }
        if native.get("type") == "array":
            return [decode(child, native["items"]) for child in value]
        return value

    return decode(payload, native_tool["function"]["parameters"])
