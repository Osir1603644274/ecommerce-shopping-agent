"""Durable asynchronous extraction of explicitly requested shopping memory."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
import base64
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import redis.asyncio as redis

from .api import memory_bff
from .settings import settings

_EXPLICIT = re.compile(
    r"(?:记住|请记得|以后(?:买|选|推荐)|从今以后|remember\b|from now on\b|in the future\b)",
    re.IGNORECASE,
)
_SENSITIVE = re.compile(
    r"(?:身份证|手机号|电话号码|银行卡|家庭住址|住址|邮箱|密码|验证码|密钥|"
    r"api[ _-]?key|access[ _-]?token|bearer|secret|病史|疾病|过敏|残疾|健康信息)"
    r"|(?:[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+)"
    r"|(?<!\d)(?:\+?86[ -]?)?1[3-9]\d{9}(?!\d)"
    r"|(?<!\d)\d(?:[ -]?\d){5,18}(?!\d)"
    r"|(?<![a-z0-9])(?:sk|pk|api)[-_][a-z0-9_-]{16,}\b"
    r"|(?<![a-z0-9])eyJ[a-z0-9_-]{10,}\.[a-z0-9_-]{10,}"
    r"(?:\.[a-z0-9_-]{10,})?\b",
    re.IGNORECASE,
)
_GROUP = "shopping-memory-v13"
logger = logging.getLogger(__name__)
_ENQUEUE_TASKS: set[asyncio.Task[str | None]] = set()
_EXTRACTION_SYSTEM_PROMPT = (
    "Extract only explicit durable shopping preferences. "
    "Return JSON {preferences:[{preferenceKind,attributeKey,normalizedValue}]}. "
    "preferenceKind must be prefer, avoid, or indifferent. "
    "Use only exact attributeKey/normalizedValue pairs from options. "
    "Return an empty list when the user did not explicitly ask to remember it. "
    "If one value can name multiple attributes and the user did not name the "
    "specific attribute, return an empty list. Never infer health, identity, "
    "recipient, or sensitive traits. Maximum 3."
)
_EXTRACTION_TIMEOUT_SECONDS = 30.0
_EXTRACTION_MAX_RETRIES = 0


@dataclass(frozen=True, slots=True)
class MemoryExtractionObservation:
    model_called: bool
    model: str | None
    outcome: str
    duration_ms: float
    usage_observed: bool
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

    def plain(self) -> dict[str, Any]:
        return {
            "modelCalled": self.model_called,
            "model": self.model,
            "outcome": self.outcome,
            "durationMs": round(self.duration_ms, 3),
            "usageObserved": self.usage_observed,
            "promptTokens": self.prompt_tokens,
            "completionTokens": self.completion_tokens,
            "totalTokens": self.total_tokens,
        }


class MemoryExtractionFailure(RuntimeError):
    def __init__(self, observation: MemoryExtractionObservation, cause: Exception):
        super().__init__("memory extraction model call failed")
        self.observation = observation
        self.cause_type = type(cause).__name__

_OTHER_RECIPIENT = re.compile(
    r"(?:爸爸|妈妈|父亲|母亲|家人|朋友|孩子|儿子|女儿|老婆|妻子|老公|丈夫|"
    r"对象|伴侣|同事|同学|领导|客户|爷爷|奶奶|外公|外婆|祖父|祖母|他|她|他们|她们)"
    r"|(?:father|mother|parent|friend|child|son|daughter|wife|husband|partner|"
    r"colleague|coworker|boss|client|grandfather|grandmother)\b|as a gift",
    re.IGNORECASE,
)
_EXPLICIT_SELF = re.compile(
    r"(?:我(?:只)?(?:喜欢|讨厌|不喜欢|偏好|不要|在意|不在意)|我的偏好|自己|自用|我用|适合我)"
    r"|(?:i prefer|i like|i dislike|for me|myself|my own)\b",
    re.IGNORECASE,
)


def is_explicit_memory_request(message: object) -> bool:
    return (
        type(message) is str
        and 1 <= len(message) <= 2000
        and _EXPLICIT.search(message) is not None
    )


def is_sensitive_memory_request(message: object) -> bool:
    return (
        type(message) is str
        and 1 <= len(message) <= 2000
        and _SENSITIVE.search(message) is not None
    )


@lru_cache(maxsize=1)
def _memory_extraction_client() -> Any:
    """One SDK request per observed extraction attempt; worker retries stay external."""
    from openai import AsyncOpenAI

    return AsyncOpenAI(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        timeout=_EXTRACTION_TIMEOUT_SECONDS,
        max_retries=_EXTRACTION_MAX_RETRIES,
    )


def explicit_memory_recipient_scope(message: object) -> str | None:
    if not is_explicit_memory_request(message):
        return None
    return current_request_recipient_scope(message)


def current_request_recipient_scope(message: object) -> str | None:
    """Fail closed for any other or mixed subject in the current request."""
    if type(message) is not str or not 1 <= len(message) <= 2000:
        return None
    assert isinstance(message, str)
    if _OTHER_RECIPIENT.search(message):
        return "other"
    return "self" if _EXPLICIT_SELF.search(message) else None


def _session_binding(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _proposal_receipt_key(job_id: str) -> str:
    return "memory:candidate:v13:proposal:" + hashlib.sha256(
        job_id.encode("utf-8")
    ).hexdigest()


async def enqueue_memory_extraction(
    *,
    browser_session_id: str,
    user_message: str,
    category_id: str,
    recipient_scope: str,
) -> str | None:
    """Queue after the final answer; never blocks or mutates that answer."""
    if not (
        settings.memory_bff_enabled
        and settings.memory_projection_client_enabled
        and recipient_scope == "self"
        and is_explicit_memory_request(user_message)
        and not is_sensitive_memory_request(user_message)
        and current_request_recipient_scope(user_message) != "other"
    ):
        return None
    if type(browser_session_id) is not str or not browser_session_id:
        return None
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", category_id or ""):
        return None
    async def persist() -> str | None:
        binding = _session_binding(browser_session_id)
        if await memory_bff.session_for_binding(binding) is None:
            return None
        job_id = secrets.token_urlsafe(18)
        client = memory_bff._redis()
        await client.xadd(
            settings.memory_candidate_stream_key,
            {
                "jobId": job_id,
                "sessionBinding": binding,
                "categoryId": category_id,
                "recipientScope": "self",
                "userMessage": user_message,
            },
            maxlen=1000,
            approximate=True,
        )
        await client.expire(settings.memory_candidate_stream_key, 86400)
        return job_id

    try:
        return await asyncio.wait_for(persist(), timeout=0.25)
    except Exception:
        logger.warning("memory candidate enqueue unavailable; final answer preserved")
        return None


def schedule_memory_extraction(**kwargs: str) -> None:
    """Schedule best-effort persistence after the user response is ready."""
    task = asyncio.create_task(enqueue_memory_extraction(**kwargs))
    _ENQUEUE_TASKS.add(task)

    def completed(done: asyncio.Task[str | None]) -> None:
        _ENQUEUE_TASKS.discard(done)
        try:
            done.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning("memory candidate scheduling failed")

    task.add_done_callback(completed)


@lru_cache(maxsize=1)
def _catalog() -> tuple[dict[str, str], ...]:
    path = Path(settings.memory_catalog_values_path)
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != settings.memory_catalog_values_sha256:
        raise ValueError("memory catalog hash mismatch")
    rows: list[dict[str, str]] = []
    for line in payload.decode("utf-8").splitlines():
        if line:
            raw = json.loads(line)
            required = {
                "catalogRevision", "categoryId", "attributeKey",
                "normalizedValue", "displayLabel",
            }
            if type(raw) is not dict or set(raw) != required:
                raise ValueError("invalid memory catalog")
            if any(type(raw[key]) is not str for key in required):
                raise ValueError("invalid memory catalog")
            if raw["catalogRevision"] != settings.memory_active_catalog_revision:
                raise ValueError("memory catalog revision mismatch")
            rows.append(raw)
    identities = [
        (row["categoryId"], row["attributeKey"], row["normalizedValue"])
        for row in rows
    ]
    if not rows or len(identities) != len(set(identities)) or identities != sorted(identities):
        raise ValueError("memory catalog identity mismatch")
    return tuple(rows)


def _shortlist(message: str, category_id: str) -> list[dict[str, str]]:
    message_folded = message.casefold()
    candidates = [row for row in _catalog() if row["categoryId"] == category_id]
    exact = [
        row for row in candidates
        if row["displayLabel"].casefold() in message_folded
        or row["normalizedValue"].replace("-", " ") in message_folded
    ]
    exact_keys = {row["attributeKey"] for row in exact}
    return sorted(
        candidates,
        key=lambda row: (
            0 if row["attributeKey"] in exact_keys else 1,
            row["attributeKey"],
            row["normalizedValue"],
        ),
    )[:60]


_ATTRIBUTE_GROUNDING = {
    "battery_originality": re.compile(r"(?:电池|battery)", re.IGNORECASE),
    "screen_originality": re.compile(r"(?:屏幕|显示屏|screen|display)", re.IGNORECASE),
}


def _ambiguous_attribute_is_grounded(
    message: str,
    row: dict[str, str],
    options: list[dict[str, str]],
) -> bool:
    competing_keys = {
        option["attributeKey"]
        for option in options
        if option["displayLabel"].casefold() == row["displayLabel"].casefold()
        or option["normalizedValue"] == row["normalizedValue"]
    }
    if len(competing_keys) <= 1:
        return True
    grounding = _ATTRIBUTE_GROUNDING.get(row["attributeKey"])
    return grounding is not None and grounding.search(message) is not None


def _message_has_ungrounded_ambiguous_value(
    message: str,
    options: list[dict[str, str]],
) -> bool:
    folded = message.casefold()
    for row in options:
        label = row["displayLabel"].casefold()
        normalized = row["normalizedValue"].replace("-", " ").casefold()
        if label not in folded and normalized not in folded:
            continue
        competing = [
            option
            for option in options
            if option["displayLabel"].casefold() == label
            or option["normalizedValue"] == row["normalizedValue"]
        ]
        if len({option["attributeKey"] for option in competing}) <= 1:
            continue
        if not any(
            _ambiguous_attribute_is_grounded(message, option, options)
            for option in competing
        ):
            return True
    return False


async def _extract_observed(
    message: str, category_id: str, recipient_scope: str,
) -> tuple[list[dict[str, str]], MemoryExtractionObservation]:
    started = time.perf_counter()

    def observation(
        outcome: str,
        *,
        called: bool = False,
        usage: Any | None = None,
    ) -> MemoryExtractionObservation:
        def token(name: str) -> int:
            value = getattr(usage, name, 0) if usage is not None else 0
            return value if type(value) is int and value >= 0 else 0

        usage_observed = usage is not None and all(
            type(getattr(usage, name, None)) is int
            and getattr(usage, name) >= 0
            for name in ("prompt_tokens", "completion_tokens", "total_tokens")
        )

        return MemoryExtractionObservation(
            model_called=called,
            model=settings.deepseek_model if called else None,
            outcome=outcome,
            duration_ms=(time.perf_counter() - started) * 1000,
            usage_observed=usage_observed,
            prompt_tokens=token("prompt_tokens"),
            completion_tokens=token("completion_tokens"),
            total_tokens=token("total_tokens"),
        )

    if not is_explicit_memory_request(message):
        return [], observation("rule_not_explicit")
    if is_sensitive_memory_request(message):
        return [], observation("sensitive_suppressed")
    if recipient_scope != "self" or current_request_recipient_scope(message) == "other":
        return [], observation("recipient_suppressed")
    options = _shortlist(message, category_id)
    if not options:
        return [], observation("catalog_options_empty")
    if _message_has_ungrounded_ambiguous_value(message, options):
        return [], observation("ambiguous_input_suppressed")
    if not settings.deepseek_api_key:
        return [], observation("model_unavailable")
    safe_options = [
        {
            "attributeKey": row["attributeKey"],
            "normalizedValue": row["normalizedValue"],
            "displayLabel": row["displayLabel"],
        }
        for row in options
    ]
    try:
        response = await _memory_extraction_client().chat.completions.create(
            model=settings.deepseek_model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"message": message, "options": safe_options},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
        )
    except Exception as exc:
        raise MemoryExtractionFailure(
            observation("model_exception", called=True), exc
        ) from exc
    usage = getattr(response, "usage", None)
    try:
        raw = json.loads(response.choices[0].message.content or "{}")
    except (AttributeError, IndexError, TypeError, json.JSONDecodeError):
        return [], observation("invalid_model_output", called=True, usage=usage)
    if type(raw) is not dict or set(raw) != {"preferences"} or type(raw["preferences"]) is not list:
        return [], observation("invalid_model_output", called=True, usage=usage)
    allowed = {
        (row["attributeKey"], row["normalizedValue"]): row for row in options
    }
    result: list[dict[str, str]] = []
    for item in raw["preferences"][:3]:
        if type(item) is not dict or set(item) != {
            "preferenceKind", "attributeKey", "normalizedValue"
        }:
            return [], observation("invalid_model_output", called=True, usage=usage)
        if item["preferenceKind"] not in {"prefer", "avoid", "indifferent"}:
            return [], observation("invalid_model_output", called=True, usage=usage)
        row = allowed.get((item["attributeKey"], item["normalizedValue"]))
        if row is None:
            return [], observation("catalog_escape_rejected", called=True, usage=usage)
        if not _ambiguous_attribute_is_grounded(message, row, options):
            return [], observation(
                "ambiguous_attribute_rejected", called=True, usage=usage
            )
        result.append({
            "categoryId": row["categoryId"],
            "preferenceKind": item["preferenceKind"],
            "attributeKey": row["attributeKey"],
            "normalizedValue": row["normalizedValue"],
            "catalogRevision": row["catalogRevision"],
            "recipientScope": recipient_scope,
            "source": "user_confirmed",
            "displayLabel": row["displayLabel"],
        })
    identities = [
        (item["preferenceKind"], item["attributeKey"], item["normalizedValue"])
        for item in result
    ]
    if len(identities) != len(set(identities)):
        return [], observation("duplicate_output_rejected", called=True, usage=usage)
    return result, observation(
        "accepted" if result else "empty",
        called=True,
        usage=usage,
    )


async def _extract(
    message: str, category_id: str, recipient_scope: str,
) -> list[dict[str, str]]:
    result, _observation = await _extract_observed(
        message, category_id, recipient_scope
    )
    return result


async def _proposals_for_job(fields: dict[str, str]) -> list[dict[str, str]]:
    """Freeze the first valid extraction so retries cannot change semantics."""
    client = memory_bff._redis()
    key = _proposal_receipt_key(fields["jobId"])
    identity = {
        "jobId": fields["jobId"],
        "sessionBinding": fields["sessionBinding"],
        "categoryId": fields["categoryId"],
        "recipientScope": fields["recipientScope"],
        "messageDigest": hashlib.sha256(
            fields["userMessage"].encode("utf-8")
        ).hexdigest(),
    }

    def parse(raw: str | bytes | None) -> list[dict[str, str]] | None:
        if raw is None:
            return None
        value = json.loads(raw)
        if type(value) is not dict or set(value) != {*identity, "proposals"}:
            raise RuntimeError("memory proposal receipt invalid")
        if any(value[name] != expected for name, expected in identity.items()):
            raise RuntimeError("memory proposal receipt identity conflict")
        if type(value["proposals"]) is not list:
            raise RuntimeError("memory proposal receipt invalid")
        required = {
            "categoryId", "preferenceKind", "attributeKey", "normalizedValue",
            "catalogRevision", "recipientScope", "source", "displayLabel",
        }
        if len(value["proposals"]) > 3 or any(
            type(item) is not dict
            or set(item) != required
            or any(type(item[name]) is not str for name in required)
            or item["categoryId"] != fields["categoryId"]
            or item["recipientScope"] != "self"
            or item["source"] != "user_confirmed"
            or item["catalogRevision"] != settings.memory_active_catalog_revision
            for item in value["proposals"]
        ):
            raise RuntimeError("memory proposal receipt invalid")
        return value["proposals"]

    lease_key = key + ":lease"
    lease_token = secrets.token_urlsafe(24)
    while True:
        existing = parse(await client.get(key))
        if existing is not None:
            return existing
        if await client.set(lease_key, lease_token, ex=60, nx=True):
            break
        deadline = asyncio.get_running_loop().time() + 61.0
        while asyncio.get_running_loop().time() < deadline:
            existing = parse(await client.get(key))
            if existing is not None:
                return existing
            if await client.get(lease_key) is None:
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("memory proposal extraction lease unavailable")

    try:
        proposals = await asyncio.wait_for(
            _extract(
                fields["userMessage"], fields["categoryId"], fields["recipientScope"]
            ), timeout=45.0
        )
        envelope = {**identity, "proposals": proposals}
        payload = json.dumps(
            envelope, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        )
        if await client.set(key, payload, ex=86400, nx=True):
            return proposals
        authoritative = parse(await client.get(key))
        if authoritative is None:
            raise RuntimeError("memory proposal receipt unavailable")
        return authoritative
    finally:
        await client.eval(
            "if redis.call('GET',KEYS[1])==ARGV[1] then "
            "return redis.call('DEL',KEYS[1]) else return 0 end",
            1, lease_key, lease_token,
        )


async def _process(fields: dict[str, str]) -> None:
    required = {
        "jobId", "sessionBinding", "categoryId", "recipientScope", "userMessage"
    }
    if type(fields) is not dict or set(fields) != required:
        return
    if fields["recipientScope"] != "self":
        return
    session = await memory_bff.session_for_binding(fields["sessionBinding"])
    if session is None:
        return
    proposals = await _proposals_for_job(fields)
    for proposal in proposals:
        display_label = proposal.pop("displayLabel")
        try:
            validated = await memory_bff._java(
                "POST", "/api/memory/catalog/validate/v3",
                access_token=session["accessToken"], body=proposal,
            )
        except Exception:
            # Leave the stream item pending. The consumer loop will reclaim it
            # and reuse the deterministic candidate id after authority recovery.
            raise
        if type(validated) is not dict or validated.get("valid") is not True:
            continue
        label = validated.get("displayLabel")
        if type(label) is not str or not label:
            continue
        kind = {"prefer": "偏好", "avoid": "不想要", "indifferent": "不在意"}[
            proposal["preferenceKind"]
        ]
        candidate_seed = json.dumps(
            {"jobId": fields["jobId"], "preference": proposal},
            ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        candidate_id = base64.urlsafe_b64encode(
            hashlib.sha256(candidate_seed).digest()
        ).decode("ascii").rstrip("=")
        stored = await memory_bff.store_validated_candidate_for_binding(
            session_binding=fields["sessionBinding"],
            preference=proposal,
            display_text=f"仅用于你本人：是否记住“{kind} {label}”？",
            candidate_id=candidate_id,
        )
        if stored is None:
            raise RuntimeError("memory candidate persistence unavailable")


async def _consume_messages(
    client: redis.Redis,
    consumer: str,
    messages: list[tuple[str, dict[str, str]]],
) -> None:
    for message_id, fields in messages:
        try:
            await _process(fields)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("memory candidate processing deferred for retry")
            continue
        await client.xack(settings.memory_candidate_stream_key, _GROUP, message_id)


async def _claim_pending(
    client: redis.Redis, consumer: str, start_id: str,
) -> str:
    claimed = await client.xautoclaim(
        settings.memory_candidate_stream_key,
        _GROUP,
        consumer,
        min_idle_time=30_000,
        start_id=start_id,
        count=10,
    )
    next_id = claimed[0] if claimed else "0-0"
    if isinstance(next_id, bytes):
        next_id = next_id.decode("ascii")
    claimed_messages = claimed[1] if len(claimed) > 1 else []
    await _consume_messages(client, consumer, list(claimed_messages))
    return next_id if type(next_id) is str and next_id else "0-0"


async def run_memory_candidate_worker(stop: asyncio.Event) -> None:
    client: redis.Redis = memory_bff._redis()
    try:
        await client.xgroup_create(
            settings.memory_candidate_stream_key, _GROUP, id="0", mkstream=True
        )
    except redis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise
    consumer = "worker-" + secrets.token_hex(6)
    claim_cursor = "0-0"
    while not stop.is_set():
        try:
            claim_cursor = await _claim_pending(client, consumer, claim_cursor)
            batches = await client.xreadgroup(
                _GROUP, consumer,
                {settings.memory_candidate_stream_key: ">"},
                count=1, block=1000,
            )
            for _stream, messages in batches:
                await _consume_messages(client, consumer, list(messages))
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(1)


__all__ = [
    "enqueue_memory_extraction",
    "current_request_recipient_scope",
    "schedule_memory_extraction",
    "is_explicit_memory_request",
    "is_sensitive_memory_request",
    "run_memory_candidate_worker",
]
