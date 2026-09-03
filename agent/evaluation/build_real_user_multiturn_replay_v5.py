"""Build the corrected pre-registered V5 real-user Context A/B package."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "agent/evaluation/real_user_multiturn_replay_20260902_v4"
TARGET = ROOT / "agent/evaluation/real_user_multiturn_replay_20260903_v5"
OLD_ID = "real_user_multiturn_replay_20260902_v4"
PACKAGE_ID = "real_user_multiturn_replay_20260903_v5"


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha_file(path: Path) -> str:
    return sha_bytes(path.read_bytes())


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def source(path: str) -> dict[str, str]:
    absolute = ROOT / path
    return {"path": path.replace("\\", "/"), "sha256": sha_file(absolute)}


def main() -> int:
    if TARGET.exists():
        raise RuntimeError(f"refusing to overwrite {TARGET}")
    TARGET.mkdir(parents=True)
    copied = (
        "ai_judge_response.schema.json",
        "build_replay_schedule.py",
        "conversation.schema.json",
        "conversations.jsonl",
        "execution_receipt.schema.json",
        "multi_agent_evaluation_contract.json",
        "paired_output.schema.json",
        "README_FOR_AI_JUDGES.md",
        "reference_context_cases.json",
        "replay_contract.json",
        "session_source_receipt.schema.json",
        "session_source_receipts.jsonl",
        "source_dedup_leakage_audit.json",
        "source_files.json",
    )
    for name in copied:
        shutil.copy2(SOURCE / name, TARGET / name)
    for name in (
        "ai_judge_response.schema.json",
        "execution_receipt.schema.json",
        "paired_output.schema.json",
    ):
        path = TARGET / name
        path.write_text(path.read_text(encoding="utf-8").replace(OLD_ID, PACKAGE_ID), encoding="utf-8", newline="\n")

    blind = (SOURCE / "blind_packet_builder.py").read_text(encoding="utf-8")
    blind = blind.replace(OLD_ID, PACKAGE_ID)
    old = '''            first_binding = rows[0]["executionBinding"]
            fixed_identity = (first_binding["runId"], first_binding["taskId"], first_binding["sessionId"])
            fixed_branch_point = first_binding["branchPointStateHash"]
            previous_post_revision: int | None = None
            for turn_index, row in enumerate(rows):
                binding = row["executionBinding"]
                if (binding["runId"], binding["taskId"], binding["sessionId"]) != fixed_identity:
                    raise ValueError("execution identity changed within a conversation arm")
'''
    new = '''            first_binding = rows[0]["executionBinding"]
            fixed_identity = (first_binding["taskId"], first_binding["sessionId"])
            fixed_branch_point = first_binding["branchPointStateHash"]
            seen_turn_run_ids: set[str] = set()
            previous_post_revision: int | None = None
            for turn_index, row in enumerate(rows):
                binding = row["executionBinding"]
                if (binding["taskId"], binding["sessionId"]) != fixed_identity:
                    raise ValueError("task/session identity changed within a conversation arm")
                if binding["runId"] in seen_turn_run_ids:
                    raise ValueError("durable runId must be unique per user turn")
                seen_turn_run_ids.add(binding["runId"])
'''
    if old not in blind:
        raise RuntimeError("V4 blind identity block not found")
    (TARGET / "blind_packet_builder.py").write_text(blind.replace(old, new), encoding="utf-8", newline="\n")

    config = {
        "schemaVersion": "real-user-multiturn-execution-config-snapshot-v5",
        "packageId": PACKAGE_ID,
        "frozenDate": "2026-09-03",
        "components": {
            "controlledWorld": {
                "snapshotId": "used-phone-439-09807c773ce6",
                "networkAccessAllowed": False,
                "catalogItemCount": 439,
                "sourceFiles": [
                    source("data/derived/ecommerce/used_phone_catalog_expansion_kuaisearch_09807c_20260823_r3/catalog.jsonl"),
                    source("data/derived/ecommerce/used_phone_catalog_expansion_kuaisearch_09807c_20260823_r3/prices.jsonl"),
                    source("data/derived/ecommerce/used_phone_catalog_expansion_kuaisearch_09807c_20260823_r3/price_manifest.json"),
                ],
            },
            "modelConfiguration": {
                "provider": "deepseek",
                "model": "deepseek-v4-flash",
                "temperature": 0,
                "maxOutputTokens": 1024,
                "toolRequestThinkingMode": "disabled",
                "sourceFiles": [
                    source("agent/app/llm.py"),
                    source("agent/app/settings.py"),
                    source("agent/evaluation/real_user_multiturn_ab_executor_20260902_v2/lane_runtime.py"),
                ],
            },
            "toolConfiguration": {
                "transport": "offline_frozen_439_catalog",
                "allowedTools": ["search_products", "get_product_details", "compare_products"],
                "sourceFiles": [
                    source("agent/evaluation/real_user_multiturn_ab_executor_20260902_v2/lane_runtime.py"),
                    source("agent/app/domains/ecommerce/models.py"),
                    source("agent/app/domains/ecommerce/ranking_contract.py"),
                    source("agent/app/domains/ecommerce/synthetic_prices.py"),
                ],
            },
            "budgetConfiguration": {
                "providerTimeoutSeconds": 30,
                "maxOutputTokensPerModelCall": 1024,
                "automaticRetries": 0,
                "requiredArmTurnOutputCount": 42,
                "failedCallReplacementAllowed": False,
                "sourceFiles": [
                    source("agent/evaluation/real_user_multiturn_ab_executor_20260902_v2/lane_runtime.py"),
                ],
            },
            "policyConfiguration": {
                "controlHistoryPolicy": "complete_same_arm_raw_dialogue",
                "treatmentHistoryPolicy": "server_compiled_context_without_raw_prior_transcript",
                "onlyArmDifference": "history_policy",
                "sameAuthoritativeStateRequired": True,
                "sameReferenceResolutionRequired": True,
                "sameCandidateScopeRequired": True,
                "stableIdentity": ["taskId", "sessionId", "branchPointStateHash"],
                "runIdentityPolicy": "unique_runId_per_user_turn",
                "multiAgentV2Enabled": False,
                "multiAgentReason": "separate experiment; disabled equally to isolate Context",
                "sourceFiles": [
                    source("agent/app/context_compiler_v1.py"),
                    source("agent/app/reference_context.py"),
                    source("agent/app/evaluation_context_arm.py"),
                    source("agent/evaluation/real_user_multiturn_ab_executor_20260902_v2/runner.py"),
                ],
            },
        },
    }
    write_json(TARGET / "execution_config_snapshot.json", config)
    field_map = {
        "controlledWorldHash": "controlledWorld",
        "modelConfigurationHash": "modelConfiguration",
        "toolConfigurationHash": "toolConfiguration",
        "budgetConfigurationHash": "budgetConfiguration",
        "policyConfigurationHash": "policyConfiguration",
    }
    expected = {
        field: hashlib.sha256(canonical(config["components"][component]).encode("utf-8")).hexdigest()
        for field, component in field_map.items()
    }
    authority = {
        "schemaVersion": "real-user-multiturn-execution-authority-v5",
        "packageId": PACKAGE_ID,
        "status": "PREREGISTERED_BEFORE_FORMAL_EXECUTION",
        "configSnapshot": {"path": "execution_config_snapshot.json", "sha256": sha_file(TARGET / "execution_config_snapshot.json")},
        "expectedTraceHashes": expected,
        "formalRunnerReceiptContract": {
            "required": True,
            "schema": "execution_receipt.schema.json",
            "ownership": "RUNNER_OWNED",
            "requiredArmTurnCount": 42,
            "mustBind": ["executionAuthoritySha256", "executionConfigSnapshotSha256", "pairedOutputsCanonicalSha256", *expected],
        },
        "repairFromV4": {
            "v4Status": "PREFORMAL_HOLD",
            "v4Failure": "one durable runId was incorrectly reused across user turns",
            "v4FormalAttemptExecuted": False,
            "preservedSmoke": "agent/evaluation/real_user_multiturn_ab_executor_20260902_v2/smoke004/result.json",
        },
        "claimBoundary": "Development paired evaluation only; no production default, production readiness, all-category or causal superiority claim.",
    }
    write_json(TARGET / "execution_authority.json", authority)
    prereg = """# Real-user multi-turn Context A/B V5 preregistration\n\n- Dataset: byte-identical V4 conversations, 8 sessions / 21 turns / 13 scored follow-ups.\n- Control: complete same-arm raw dialogue.\n- Treatment: zero raw prior transcript; current server Context compiler/view only.\n- Both arms: current react_v1, frozen 439-product offline tools, DeepSeek v4 flash, temperature 0, 1024 max output tokens, no automatic retry, Multi-Agent disabled equally.\n- Identity: taskId/sessionId remain stable within an arm; every user turn receives a new durable runId.\n- Formal execution: exactly one 42-output attempt. Failed output is preserved and never replaced in this package.\n- Primary quality gate: all 42 turns complete, 13 follow-ups receive blind paired judgments, and treatment may not lose on task correctness/safety.\n- Efficiency is descriptive unless provider usage is observed. No default switch follows automatically.\n"""
    (TARGET / "preregistration.md").write_text(prereg, encoding="utf-8", newline="\n")

    verify = '''from __future__ import annotations
import hashlib,json
from pathlib import Path

P=Path(__file__).resolve().parent
ROOT=P.parents[2]
PID="real_user_multiturn_replay_20260903_v5"
def canon(x): return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(",",":"))
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
    cfg=json.loads((P/"execution_config_snapshot.json").read_text(encoding="utf-8"))
    auth=json.loads((P/"execution_authority.json").read_text(encoding="utf-8"))
    assert cfg["packageId"]==auth["packageId"]==PID
    assert auth["configSnapshot"]["sha256"]==sha(P/"execution_config_snapshot.json")
    fm={"controlledWorldHash":"controlledWorld","modelConfigurationHash":"modelConfiguration","toolConfigurationHash":"toolConfiguration","budgetConfigurationHash":"budgetConfiguration","policyConfigurationHash":"policyConfiguration"}
    for field,component in fm.items():
        assert auth["expectedTraceHashes"][field]==hashlib.sha256(canon(cfg["components"][component]).encode()).hexdigest()
        for src in cfg["components"][component]["sourceFiles"]:
            assert sha(ROOT/src["path"])==src["sha256"]
    conversations=[json.loads(x) for x in (P/"conversations.jsonl").read_text(encoding="utf-8").splitlines() if x]
    assert len(conversations)==8 and sum(len(x["turns"]) for x in conversations)==21
    sums={line.split("  ",1)[1]:line.split("  ",1)[0] for line in (P/"SHA256SUMS.txt").read_text().splitlines() if line}
    assert all(sha(P/name)==digest for name,digest in sums.items())
    result={"status":"PASS","packageId":PID,"conversationCount":8,"turnCount":21,"scheduledArmTurns":42,"sourceHashFailures":0,"checksumFailures":0,"formalAttempts":0}
    (P/"verification.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\\n",encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False))
    return 0
if __name__=="__main__": raise SystemExit(main())
'''
    (TARGET / "verify_package.py").write_text(verify, encoding="utf-8", newline="\n")
    readme = """# Real-user multi-turn Context A/B V5\n\nStatus before execution: `PREREGISTERED_READY / NO_FORMAL_ATTEMPT`.\n\nV5 preserves the V4 real-user dataset and repairs two preformal defects: durable `runId` is unique per user turn, and the evaluation receipt serializer supports slotted dataclasses. Multi-Agent is disabled equally in both arms to isolate Context.\n"""
    (TARGET / "README.md").write_text(readme, encoding="utf-8", newline="\n")
    inventory = sorted(path for path in TARGET.iterdir() if path.is_file() and path.name not in {"SHA256SUMS.txt", "verification.json"})
    (TARGET / "SHA256SUMS.txt").write_text(
        "".join(f"{sha_file(path)}  {path.name}\n" for path in inventory), encoding="utf-8", newline="\n"
    )
    manifest = {
        "schemaVersion": "real-user-multiturn-package-manifest-v5",
        "packageId": PACKAGE_ID,
        "status": "PREREGISTERED_READY_NO_FORMAL_ATTEMPT",
        "parentDatasetPackage": OLD_ID,
        "conversationCount": 8,
        "turnCount": 21,
        "pairedOutputCount": 42,
        "files": {path.name: sha_file(path) for path in inventory},
    }
    write_json(TARGET / "package_manifest.json", manifest)
    # Rebuild checksum inventory once after the manifest exists.
    inventory = sorted(path for path in TARGET.iterdir() if path.is_file() and path.name not in {"SHA256SUMS.txt", "verification.json"})
    (TARGET / "SHA256SUMS.txt").write_text(
        "".join(f"{sha_file(path)}  {path.name}\n" for path in inventory), encoding="utf-8", newline="\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
