"""Real Redis/Java/BFF asynchronous memory-worker supplement without a model.

The production worker, stream group, proposal receipt, Java catalog authority,
candidate store and BFF visibility are exercised unchanged.  The sole semantic
test double is ``memory_candidate_worker._extract``: it returns one frozen
catalog tuple per explicit message and never calls a model.  Existing attempt
directories are immutable.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


AGENT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = AGENT_ROOT.parent
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

from evaluation import shopping_memory_v13_real_infra_three_session_v2 as e2e


SCHEMA = "shopping-memory-v13-real-worker-no-model-v1"
VERDICT = "REAL_INFRA_NO_MODEL_WORKER_RETRY_ACCEPT"
HOLD = "HOLD_REAL_INFRA_WORKER_FAILED"
CATALOG_REVISION = e2e.CATALOG_REVISION
GROUP = "shopping-memory-v13"
FIXTURES = {
    "请记住我喜欢原装屏，适合我自己": {
        "categoryId": "phone",
        "preferenceKind": "prefer",
        "attributeKey": "screen_originality",
        "normalizedValue": "original",
        "catalogRevision": CATALOG_REVISION,
        "recipientScope": "self",
        "source": "user_confirmed",
        "displayLabel": "原装",
    },
    "请记住我喜欢原装电池，适合我自己": {
        "categoryId": "phone",
        "preferenceKind": "prefer",
        "attributeKey": "battery_originality",
        "normalizedValue": "original",
        "catalogRevision": CATALOG_REVISION,
        "recipientScope": "self",
        "source": "user_confirmed",
        "displayLabel": "原装",
    },
}


class RunFailure(RuntimeError):
    pass


def candidate_id_for(job_id: str, proposal: dict[str, str]) -> str:
    preference = {key: value for key, value in proposal.items() if key != "displayLabel"}
    seed = json.dumps(
        {"jobId": job_id, "preference": preference},
        ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(hashlib.sha256(seed).digest()).decode("ascii").rstrip("=")


def verdict_for_checks(checks: dict[str, bool]) -> str:
    return VERDICT if checks and all(value is True for value in checks.values()) else HOLD


def configure_process(args: argparse.Namespace) -> None:
    e2e.base.configure_process(
        backend_url=args.backend_url, redis_url=args.redis_url, username=args.username,
    )
    os.environ["MEMORY_CANDIDATE_STREAM_KEY"] = args.stream_key


async def _pending_count(client: object, stream: str) -> int:
    value = await client.xpending(stream, GROUP)
    if type(value) is dict:
        return int(value.get("pending", 0))
    if isinstance(value, (list, tuple)) and value:
        return int(value[0])
    raise RunFailure("unexpected XPENDING response")


async def _read_one(client: object, stream: str, consumer: str) -> tuple[str, dict[str, str]]:
    batches = await client.xreadgroup(
        GROUP, consumer, {stream: ">"}, count=1, block=2000,
    )
    if len(batches) != 1 or len(batches[0][1]) != 1:
        raise RunFailure("XREADGROUP did not return exactly one worker item")
    message_id, fields = batches[0][1][0]
    if type(message_id) is not str or type(fields) is not dict:
        raise RunFailure("XREADGROUP returned an invalid item")
    return message_id, fields


def _receipt_summary(
    raw: str, expected_job: str, expected_message: str,
) -> dict[str, Any]:
    value = json.loads(raw)
    if (
        type(value) is not dict
        or set(value) != {
            "jobId", "sessionBinding", "categoryId", "recipientScope",
            "messageDigest", "proposals",
        }
        or value.get("jobId") != expected_job
        or value.get("categoryId") != "phone"
        or value.get("recipientScope") != "self"
        or value.get("messageDigest") != e2e.base.digest_text(expected_message)
        or type(value.get("sessionBinding")) is not str
        or len(value["sessionBinding"]) != 64
    ):
        raise RunFailure("proposal receipt identity mismatch")
    proposals = value.get("proposals")
    if type(proposals) is not list or len(proposals) != 1:
        raise RunFailure("proposal receipt did not freeze exactly one proposal")
    proposal = proposals[0]
    if (
        type(proposal) is not dict
        or proposal != FIXTURES.get(expected_message)
    ):
        raise RunFailure("proposal receipt shape mismatch")
    return {
        "receiptSha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "messageDigest": value.get("messageDigest"),
        "proposalCount": 1,
        "proposal": {
            "categoryId": proposal["categoryId"],
            "preferenceKind": proposal["preferenceKind"],
            "attributeKey": proposal["attributeKey"],
            "normalizedValue": proposal["normalizedValue"],
            "catalogRevision": proposal["catalogRevision"],
            "recipientScope": proposal["recipientScope"],
            "source": proposal["source"],
        },
    }


async def execute(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    settings_path = AGENT_ROOT / "app/settings.py"
    settings_sha_before = e2e.base.sha256_file(settings_path)
    defaults = e2e.settings_defaults_from_source(settings_path)
    if settings_sha_before != e2e.EXPECTED_SETTINGS_SHA256 or defaults != {
        "memory_bff_enabled": False,
        "memory_projection_client_enabled": False,
    }:
        raise RunFailure("frozen production defaults are not disabled")
    configure_process(args)

    import redis.asyncio as redis
    from redis.exceptions import ResponseError
    from app import memory_candidate_worker as worker
    from app.api import memory_bff
    from app.main import app
    from app.settings import settings

    if worker._GROUP != GROUP or settings.memory_candidate_stream_key != args.stream_key:
        raise RunFailure("worker stream/group binding mismatch")
    started_at = datetime.now(timezone.utc).isoformat()
    runner_path = Path(__file__).resolve()
    source_paths = [
        runner_path,
        Path(e2e.__file__).resolve(),
        Path(e2e.base.__file__).resolve(),
        AGENT_ROOT / "app/memory_candidate_worker.py",
        AGENT_ROOT / "app/api/memory_bff.py",
        AGENT_ROOT / "app/main.py",
        AGENT_ROOT / "app/settings.py",
        AGENT_ROOT / "app/llm.py",
    ]
    source_before = {
        str(path.relative_to(REPOSITORY_ROOT)): e2e.base.sha256_file(path)
        for path in source_paths
    }
    infrastructure = e2e.inspect_database_binding(args)
    database_authority = e2e.inspect_database_authority(args)
    model_tripwire, restore_tripwire = e2e.install_model_tripwire()
    original_extract = worker._extract
    original_java = memory_bff._java
    redis_client = redis.from_url(args.redis_url, decode_responses=True)
    client: httpx.AsyncClient | None = None
    runtime_secrets: set[str] = set()
    extraction_calls: dict[str, int] = {message: 0 for message in FIXTURES}
    lease_observations: dict[str, dict[str, Any]] = {}
    jobs_by_message: dict[str, str] = {}
    real_java_catalog_calls = 0
    injected_java_failures = 0

    async def counting_java(method: str, path: str, **kwargs: Any) -> Any:
        nonlocal real_java_catalog_calls
        result = await original_java(method, path, **kwargs)
        if path == "/api/memory/catalog/validate/v3":
            real_java_catalog_calls += 1
        return result

    async def deterministic_extract(
        message: str, category_id: str, recipient_scope: str,
    ) -> list[dict[str, str]]:
        if message not in FIXTURES or category_id != "phone" or recipient_scope != "self":
            raise RunFailure("deterministic extractor received an unexpected job")
        extraction_calls[message] += 1
        job_id = jobs_by_message.get(message)
        if job_id is None:
            raise RunFailure("extractor ran before job identity was recorded")
        lease_key = worker._proposal_receipt_key(job_id) + ":lease"
        lease_value = await redis_client.get(lease_key)
        lease_observations[message] = {
            "observedDuringExtraction": type(lease_value) is str and bool(lease_value),
            "ttlDuringExtraction": await redis_client.ttl(lease_key),
        }
        return [dict(FIXTURES[message])]

    worker._extract = deterministic_extract
    memory_bff._java = counting_java
    try:
        if await redis_client.ping() is not True:
            raise RunFailure("Redis ping failed")
        if await redis_client.exists(args.stream_key):
            raise RunFailure("immutable worker stream key already exists")
        try:
            await redis_client.xgroup_create(args.stream_key, GROUP, id="0", mkstream=True)
        except ResponseError as exc:
            raise RunFailure("worker consumer group already exists") from exc

        async with httpx.AsyncClient(base_url=args.backend_url, timeout=5.0) as java:
            health = await java.get("/actuator/health")
            if health.status_code != 200 or health.json().get("status") != "UP":
                raise RunFailure("Java actuator is not UP")
            password = os.environ.get(args.password_env)
            generated = password is None
            if password is None:
                password = "Mv13!" + secrets.token_urlsafe(24)
            runtime_secrets.add(password)
            registration = await java.post(
                "/api/auth/register", json={"username": args.username, "password": password},
            )
            if registration.status_code == 201:
                registered = True
            elif registration.status_code == 409 and not generated:
                registered = False
            elif registration.status_code == 409:
                raise RunFailure("worker canary already exists; provide its password env")
            else:
                raise RunFailure(f"registration returned HTTP {registration.status_code}")

        client, cookie, csrf = await e2e._login(app, args.username, password)
        runtime_secrets.update({cookie, csrf})
        session = await memory_bff.session_for_binding(e2e.base.digest_text(cookie))
        if session is None:
            raise RunFailure("BFF session was not persisted in real Redis")
        runtime_secrets.update(
            value for key, value in session.items()
            if key in {"accessToken", "refreshToken"} and type(value) is str
        )

        consumer_success = "worker-e2e-success-" + secrets.token_hex(6)
        success_message = next(iter(FIXTURES))
        success_job = await worker.enqueue_memory_extraction(
            browser_session_id=cookie,
            user_message=success_message,
            category_id="phone",
            recipient_scope="self",
        )
        if type(success_job) is not str:
            raise RunFailure("success job was not enqueued")
        jobs_by_message[success_message] = success_job
        runtime_secrets.add(success_job)
        success_message_id, success_fields = await _read_one(
            redis_client, args.stream_key, consumer_success,
        )
        pending_before_success = await _pending_count(redis_client, args.stream_key)
        await worker._consume_messages(
            redis_client, consumer_success, [(success_message_id, success_fields)],
        )
        pending_after_success = await _pending_count(redis_client, args.stream_key)
        already_acked_success = await redis_client.xack(
            args.stream_key, GROUP, success_message_id,
        )
        success_receipt_key = worker._proposal_receipt_key(success_job)
        success_receipt_raw = await redis_client.get(success_receipt_key)
        if type(success_receipt_raw) is not str:
            raise RunFailure("success proposal receipt was not persisted")
        success_receipt_before_retry = _receipt_summary(
            success_receipt_raw, success_job, success_message,
        )
        success_receipt_ttl = await redis_client.ttl(success_receipt_key)
        success_receipt_before_retry["ttlSeconds"] = success_receipt_ttl
        success_lease_after = await redis_client.exists(success_receipt_key + ":lease")
        success_expected_candidate = candidate_id_for(success_job, FIXTURES[success_message])
        runtime_secrets.add(success_expected_candidate)
        cards_after_success = await e2e._expect(
            await client.get("/api/web-memory/candidates"), 200, "worker cards after success",
        )
        e2e.assert_no_authority_fields(cards_after_success)
        success_cards = cards_after_success.get("candidates")
        if not (
            type(success_cards) is list and len(success_cards) == 1
            and success_cards[0].get("candidateId") == success_expected_candidate
            and success_cards[0].get("status") == "pending"
        ):
            raise RunFailure("successful worker item did not expose one pending BFF card")

        success_extract_calls_before_retry = extraction_calls[success_message]
        await worker._process(dict(success_fields))
        success_receipt_raw_after = await redis_client.get(success_receipt_key)
        cards_after_retry = await e2e._expect(
            await client.get("/api/web-memory/candidates"), 200, "worker cards after same-job retry",
        )
        success_cards_after_retry = cards_after_retry.get("candidates")
        if not (
            success_receipt_raw_after == success_receipt_raw
            and extraction_calls[success_message] == success_extract_calls_before_retry == 1
            and type(success_cards_after_retry) is list
            and len(success_cards_after_retry) == 1
            and success_cards_after_retry[0].get("candidateId") == success_expected_candidate
        ):
            raise RunFailure("same-job retry was not receipt/candidate idempotent")

        failure_message = list(FIXTURES)[1]
        failure_job = await worker.enqueue_memory_extraction(
            browser_session_id=cookie,
            user_message=failure_message,
            category_id="phone",
            recipient_scope="self",
        )
        if type(failure_job) is not str:
            raise RunFailure("failure/recovery job was not enqueued")
        jobs_by_message[failure_message] = failure_job
        runtime_secrets.add(failure_job)
        consumer_failure = "worker-e2e-failure-" + secrets.token_hex(6)
        failure_message_id, failure_fields = await _read_one(
            redis_client, args.stream_key, consumer_failure,
        )
        async def one_shot_java_failure(method: str, path: str, **kwargs: Any) -> Any:
            nonlocal injected_java_failures
            if path == "/api/memory/catalog/validate/v3" and injected_java_failures == 0:
                injected_java_failures += 1
                raise httpx.ConnectError("injected transient Java catalog failure")
            return await counting_java(method, path, **kwargs)

        memory_bff._java = one_shot_java_failure
        try:
            await worker._consume_messages(
                redis_client, consumer_failure, [(failure_message_id, failure_fields)],
            )
        finally:
            memory_bff._java = counting_java
        pending_after_failure = await _pending_count(redis_client, args.stream_key)
        pending_rows = await redis_client.xpending_range(
            args.stream_key, GROUP, min="-", max="+", count=10,
        )
        failure_receipt_key = worker._proposal_receipt_key(failure_job)
        failure_receipt_raw = await redis_client.get(failure_receipt_key)
        if type(failure_receipt_raw) is not str:
            raise RunFailure("failed delivery did not freeze a proposal receipt")
        failure_receipt_before_recovery = _receipt_summary(
            failure_receipt_raw, failure_job, failure_message,
        )
        failure_receipt_ttl = await redis_client.ttl(failure_receipt_key)
        failure_receipt_before_recovery["ttlSeconds"] = failure_receipt_ttl
        failure_lease_after = await redis_client.exists(failure_receipt_key + ":lease")
        failure_expected_candidate = candidate_id_for(failure_job, FIXTURES[failure_message])
        runtime_secrets.add(failure_expected_candidate)
        cards_while_pending = await e2e._expect(
            await client.get("/api/web-memory/candidates"), 200, "worker cards while Java failed",
        )
        if not (
            pending_after_failure == 1
            and len(pending_rows) == 1
            and pending_rows[0].get("message_id") == failure_message_id
            and extraction_calls[failure_message] == 1
            and all(
                item.get("candidateId") != failure_expected_candidate
                for item in cards_while_pending.get("candidates", [])
            )
        ):
            raise RunFailure("transient Java failure did not retain exactly one pending item")

        forced_idle = await redis_client.xclaim(
            args.stream_key, GROUP, consumer_failure,
            min_idle_time=0,
            message_ids=[failure_message_id],
            idle=31000,
            justid=True,
        )
        if failure_message_id not in list(forced_idle):
            raise RunFailure("failed item could not be aged for XAUTOCLAIM")
        aged_pending_rows = await redis_client.xpending_range(
            args.stream_key, GROUP, min="-", max="+", count=10, idle=30000,
        )
        if not (
            len(aged_pending_rows) == 1
            and aged_pending_rows[0].get("message_id") == failure_message_id
            and aged_pending_rows[0].get("time_since_delivered", 0) >= 30000
        ):
            raise RunFailure("failed item was not eligible for production XAUTOCLAIM")
        consumer_recovery = "worker-e2e-recovery-" + secrets.token_hex(6)
        claim_cursor = await worker._claim_pending(
            redis_client, consumer_recovery, "0-0",
        )
        pending_after_recovery = await _pending_count(redis_client, args.stream_key)
        consumer_info = await redis_client.xinfo_consumers(args.stream_key, GROUP)
        recovery_consumer_seen = any(
            item.get("name") == consumer_recovery and item.get("pending") == 0
            for item in consumer_info
        )
        already_acked_recovery = await redis_client.xack(
            args.stream_key, GROUP, failure_message_id,
        )
        failure_receipt_raw_after = await redis_client.get(failure_receipt_key)
        failure_lease_final = await redis_client.exists(failure_receipt_key + ":lease")
        cards_after_recovery = await e2e._expect(
            await client.get("/api/web-memory/candidates"), 200, "worker cards after recovery",
        )
        e2e.assert_no_authority_fields(cards_after_recovery)
        final_candidates = cards_after_recovery.get("candidates")
        final_ids = [item.get("candidateId") for item in final_candidates]
        if not (
            pending_after_recovery == 0
            and already_acked_recovery == 0
            and failure_receipt_raw_after == failure_receipt_raw
            and extraction_calls[failure_message] == 1
            and type(final_candidates) is list and len(final_candidates) == 2
            and sorted(final_ids) == sorted([success_expected_candidate, failure_expected_candidate])
            and all(item.get("status") == "pending" for item in final_candidates)
        ):
            raise RunFailure("XAUTOCLAIM recovery was not idempotent and ACKed")

        source_after = {
            str(path.relative_to(REPOSITORY_ROOT)): e2e.base.sha256_file(path)
            for path in source_paths
        }
        source_current = {
            str(path.relative_to(REPOSITORY_ROOT)): e2e.base.sha256_file(path)
            for path in source_paths
        }
        settings_sha_after = e2e.base.sha256_file(settings_path)
        measured_model_calls = (
            model_tripwire["getClientAttempts"]
            + model_tripwire["modelObservationAttempts"]
        )
        checks = {
            "javaHealthy": True,
            "realRedisPing": True,
            **infrastructure["checks"],
            **database_authority["checks"],
            "realStreamConsumerGroup": True,
            "successEnteredPendingBeforeProcessing": pending_before_success == 1,
            "successXacked": pending_after_success == 0 and already_acked_success == 0,
            "successPendingCardVisibleThroughRealBff": True,
            "successProposalReceiptFrozen": success_receipt_raw_after == success_receipt_raw,
            "successProposalReceiptTtlBounded": 0 < success_receipt_ttl <= 86400,
            "successLeaseObservedAndReleased": (
                lease_observations[success_message]["observedDuringExtraction"]
                and lease_observations[success_message]["ttlDuringExtraction"] > 0
                and success_lease_after == 0
            ),
            "sameJobRetrySkippedExtraction": extraction_calls[success_message] == 1,
            "sameJobCandidateIdStableAndUnique": len(success_cards_after_retry) == 1,
            "transientJavaFailureInjectedOnce": injected_java_failures == 1,
            "failedMessageRemainedPending": pending_after_failure == 1,
            "failedDeliveryCreatedNoCard": failure_expected_candidate not in [
                item.get("candidateId") for item in cards_while_pending.get("candidates", [])
            ],
            "failureProposalReceiptFrozen": failure_receipt_raw_after == failure_receipt_raw,
            "failureProposalReceiptTtlBounded": 0 < failure_receipt_ttl <= 86400,
            "failureLeaseObservedAndReleased": (
                lease_observations[failure_message]["observedDuringExtraction"]
                and lease_observations[failure_message]["ttlDuringExtraction"] > 0
                and failure_lease_after == 0 and failure_lease_final == 0
            ),
            "xautoclaimRecovered": (
                pending_after_recovery == 0
                and claim_cursor == "0-0"
                and recovery_consumer_seen
            ),
            "recoveryXacked": already_acked_recovery == 0,
            "recoverySkippedDuplicateExtraction": extraction_calls[failure_message] == 1,
            "recoveryCandidateIdStableAndUnique": final_ids.count(failure_expected_candidate) == 1,
            "twoPendingCardsVisibleThroughRealBff": len(final_candidates) == 2,
            "realJavaCatalogValidationCalls": real_java_catalog_calls == 3,
            "deterministicExtractorInstalled": worker._extract is deterministic_extract,
            "modelCallsZero": measured_model_calls == 0,
            "productionDefaultsRemainOffOnDisk": defaults == {
                "memory_bff_enabled": False,
                "memory_projection_client_enabled": False,
            },
            "settingsSourceHashStable": settings_sha_before == settings_sha_after == e2e.EXPECTED_SETTINGS_SHA256,
            "boundSourceHashesStable": source_before == source_after == source_current,
        }
        decision = verdict_for_checks(checks)
        trace = {
            "schemaVersion": SCHEMA,
            "attemptId": args.attempt_id,
            "startedAt": started_at,
            "finishedAt": datetime.now(timezone.utc).isoformat(),
            "scope": {
                "included": [
                    "real_bff_asgi_login_and_candidate_visibility",
                    "real_redis_stream_consumer_group_pending_ack_xautoclaim",
                    "production_worker_process_consume_claim_and_proposal_receipt_helpers",
                    "real_java_catalog_validation",
                    "production_redis_candidate_store",
                    "one_injected_transient_java_catalog_failure",
                ],
                "testDouble": {
                    "target": "app.memory_candidate_worker._extract",
                    "behavior": "deterministic_single_frozen_catalog_tuple_per_message",
                    "modelCalls": measured_model_calls,
                },
                "notEvaluated": [
                    "real_llm_extraction", "llm_quality", "browser_rendering",
                    "candidate_confirmation", "consent_command_projection",
                    "full_chat_agent", "real_product_retrieval", "default_enablement",
                    "full_run_memory_candidate_worker_loop",
                ],
            },
            "infrastructure": {
                "javaBaseUrl": args.backend_url,
                "redisUrl": args.redis_url,
                "streamKey": args.stream_key,
                "streamKeySha256": e2e.base.digest_text(args.stream_key),
                "consumerGroup": GROUP,
                "accountRegisteredThisRun": registered,
                "binding": infrastructure,
            },
            "databaseAuthority": database_authority,
            "successPath": {
                "jobIdSha256": e2e.base.digest_text(success_job),
                "messageIdSha256": e2e.base.digest_text(success_message_id),
                "pendingBeforeProcess": pending_before_success,
                "pendingAfterProcess": pending_after_success,
                "secondAckResult": already_acked_success,
                "candidateIdSha256": e2e.base.digest_text(success_expected_candidate),
                "candidateStatus": "pending",
                "extractCallsBeforeRetry": success_extract_calls_before_retry,
                "extractCallsAfterRetry": extraction_calls[success_message],
                "receipt": success_receipt_before_retry,
                "receiptKeySha256": e2e.base.digest_text(success_receipt_key),
                "receiptUnchangedAfterRetry": success_receipt_raw_after == success_receipt_raw,
                "lease": {
                    **lease_observations[success_message],
                    "existsAfterProcess": bool(success_lease_after),
                },
            },
            "failureRecoveryPath": {
                "jobIdSha256": e2e.base.digest_text(failure_job),
                "messageIdSha256": e2e.base.digest_text(failure_message_id),
                "injectedJavaFailures": injected_java_failures,
                "pendingAfterFailure": pending_after_failure,
                "pendingOwnerMatchedFailureConsumer": pending_rows[0].get("consumer") == consumer_failure,
                "cardVisibleBeforeRecovery": False,
                "forcedIdleMs": 31000,
                "eligiblePendingCountBeforeXautoclaim": len(aged_pending_rows),
                "xautoclaimCursor": claim_cursor,
                "recoveryConsumerObservedAfterAck": recovery_consumer_seen,
                "pendingAfterRecovery": pending_after_recovery,
                "secondAckResult": already_acked_recovery,
                "candidateIdSha256": e2e.base.digest_text(failure_expected_candidate),
                "candidateStatus": "pending",
                "extractCallsBeforeRecovery": 1,
                "extractCallsAfterRecovery": extraction_calls[failure_message],
                "receipt": failure_receipt_before_recovery,
                "receiptKeySha256": e2e.base.digest_text(failure_receipt_key),
                "receiptUnchangedAfterRecovery": failure_receipt_raw_after == failure_receipt_raw,
                "lease": {
                    **lease_observations[failure_message],
                    "existsAfterFailure": bool(failure_lease_after),
                    "existsAfterRecovery": bool(failure_lease_final),
                },
            },
            "finalCandidateCount": len(final_candidates),
            "realJavaCatalogValidationCalls": real_java_catalog_calls,
            "modelTripwire": model_tripwire,
            "productionDefaultsFromSource": defaults,
            "settingsSourceSha256": {"before": settings_sha_before, "after": settings_sha_after},
            "checks": checks,
            "decision": decision,
        }
        report = {
            "schemaVersion": f"{SCHEMA}-report",
            "attemptId": args.attempt_id,
            "decision": decision,
            "checks": checks,
            "modelCalls": measured_model_calls,
            "extractCalls": sum(extraction_calls.values()),
            "finalPendingCardCount": len(final_candidates),
            "limits": [
                "DETERMINISTIC_EXTRACT_TEST_DOUBLE", "NO_REAL_LLM_EXTRACTION",
                "NO_LLM_QUALITY_CLAIM", "NO_BROWSER_RENDER", "NO_FULL_CHAT_AGENT",
                "NO_CANDIDATE_CONFIRMATION", "NO_DEFAULT_SWITCH", "NO_REAL_RETRIEVAL",
                "NO_FULL_WORKER_LOOP",
            ],
        }
        receipt = {
            "schemaVersion": f"{SCHEMA}-receipt",
            "attemptId": args.attempt_id,
            "decision": decision,
            "runnerSha256": source_current[str(runner_path.relative_to(REPOSITORY_ROOT))],
            "sourceSha256Before": source_before,
            "sourceSha256After": source_after,
            "sourceSha256Current": source_current,
            "inputSha256": {
                str(e2e.base.CATALOG_VALUES.relative_to(REPOSITORY_ROOT)): e2e.base.EXPECTED_VALUES_SHA256,
            },
            "infrastructureBinding": {
                "javaContainerId": infrastructure["javaContainerId"],
                "javaImageId": infrastructure["javaImageId"],
                "javaJarSha256": infrastructure["javaJarSha256"],
                "hostPortBinding": infrastructure["hostPortBinding"],
                "redisUrl": args.redis_url,
                "streamKey": args.stream_key,
                "streamKeySha256": e2e.base.digest_text(args.stream_key),
                "consumerGroup": GROUP,
            },
            "artifactSha256": {},
        }
        for artifact in (trace, report, receipt):
            e2e.assert_artifact_safe(artifact)
            e2e.assert_no_secret_values(artifact, runtime_secrets)
        return trace, {"report": report, "receipt": receipt}
    finally:
        memory_bff._java = original_java
        worker._extract = original_extract
        restore_tripwire()
        if client is not None:
            await client.aclose()
        await redis_client.aclose()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend-url", default="http://127.0.0.1:18084")
    parser.add_argument("--redis-url", default="redis://127.0.0.1:16381/0")
    parser.add_argument("--java-container", default="agent-memory-v13-e2e-java-20260830")
    parser.add_argument("--mysql-container", default="agent-memory-v13-e2e-mysql-20260830")
    parser.add_argument(
        "--java-jar-path",
        default=str(REPOSITORY_ROOT / "backend/target/local-life-backend-0.1.0-SNAPSHOT.jar"),
    )
    parser.add_argument("--database-name", default="memory_v13_e2e")
    parser.add_argument("--username", default="memory-v13-worker-e2e-a002")
    parser.add_argument("--password-env", default="MEMORY_V13_WORKER_E2E_A002_PASSWORD")
    parser.add_argument(
        "--stream-key",
        default="memory:candidate:v13:e2e:worker-attempt002-20260830",
    )
    parser.add_argument(
        "--attempt-id",
        default="shopping_memory_v13_real_worker_no_model_v1_attempt002_20260830",
    )
    parser.add_argument(
        "--output-dir", type=Path,
        default=AGENT_ROOT / "evaluation/results/shopping_memory_v13_real_worker_no_model_v1_attempt002_20260830",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        e2e.base.create_attempt_dir(args.output_dir)
    except FileExistsError:
        print(f"attempt directory already exists: {args.output_dir}", file=sys.stderr)
        return 2
    try:
        trace, bundle = asyncio.run(execute(args))
        e2e.base.materialize(args.output_dir, trace, bundle)
        print(json.dumps({
            "decision": bundle["report"]["decision"],
            "outputDir": str(args.output_dir),
        }, ensure_ascii=False))
        return 0 if bundle["report"]["decision"] == VERDICT else 1
    except Exception as exc:
        failure = {
            "schemaVersion": f"{SCHEMA}-failure",
            "attemptId": args.attempt_id,
            "decision": HOLD,
            "errorType": type(exc).__name__,
            "error": str(exc),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            e2e.assert_artifact_safe(failure)
            e2e.base.write_json_exclusive(args.output_dir / "failure.json", failure)
        except Exception:
            pass
        print(f"worker E2E failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
