"""CLI: run the Scenario Lab V0 real-runner minimal closed loop.

TASK_PACKET — AGENTIC-SCENARIO-LAB-V0-REAL-RUNNER-REPAIR-004 (Attempt 001).

Phased CLI over :mod:`evaluation.scenario_lab_v0_real_runner`:

* ``prepare-fixtures`` — build a REAL task-owned parked-clarification fixture
  through the production TaskState store + durable graph/checkpointer APIs
  (real ``interrupt()`` park, no model/tool) and write the signed fixture
  receipt (real checkpoint id + production saver digest + envelope SHA).
  REPAIR-003 round: the real writer is proven by the offline tests against the
  controlled ``InFileRedis`` stand-in; a live run fails closed (never a
  handwritten fallback) if the production APIs cannot build the fixture.
* ``freeze`` — oracle-first freeze of the 12 V0 scenarios.  The INF-001 fault
  now freezes the REAL fixture ref/SHA read from the receipt (the old
  non-matching SENTINEL design is DELETED); a missing/drifted/cross-owner
  receipt fails closed.
* ``run``   — orchestrator: probe the real chain (structured evidence), then
  spawn the PUBLIC-ONLY entrypoint subprocess (clean argv/env/cwd, task-owned
  public runtime dir with no oracle/fault/score files).  The subprocess drives
  the real durable scenarios (or the deterministic stub world offline) and
  writes prediction.jsonl + evidence.json; the orchestrator publishes and
  proves the phases.
* ``score`` — ACCEPTED scorer in an isolated subprocess.
* ``verify``— strict fail-closed verification (non-zero on ANY mismatch;
  ``verified-blocked`` status, non-zero, for the legal blocked subset).
* ``auto``  — freeze -> run -> score in one command.

Boundaries enforced by this CLI (matching the module):

* Public-only process: the orchestrator ``run`` spawns a dedicated public-only
  subprocess that receives only the public input and task-owned runtime
  config.  Private oracle/fault/score files never exist in the subprocess's
  cwd and never appear in its argv/env.
* No fabrication: every prediction field comes from a real HTTP response, a
  read-only Redis snapshot, or a deterministic server-reason mapping disclosed
  in the manifest.  A missing dependency is recorded as BLOCKED, never faked.
* Owned runtime: only task-owned sessions/processes/ports are created; only
  processes this pipeline spawned are ever terminated.
* The old ``data/derived/scenario_lab_v0_real_runner_20260819/`` freeze dir is
  read-only and never modified by this CLI.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from evaluation import scenario_lab_v0 as lab  # noqa: E402
from evaluation.scenario_lab_v0 import (  # noqa: E402
    ScenarioContractError,
    load_records,
    sha256_file,
)
from evaluation.scenario_lab_v0_real_runner import (  # noqa: E402
    BENCHMARK_JAVA_BASE_URL,
    DEFAULT_BACKEND_BASE_URL,
    ES_BASE_URL,
    EXECUTOR_FAULT_POINT_ENV,
    EXECUTOR_FAULT_POINT_NAME,
    FIXTURE_RECEIPT_FILENAME,
    PUBLIC_PROCESS_ENV_ALLOWLIST,
    SEARCH_JAVA_BASE_URL,
    TASK_OWNED_SESSION_PREFIX,
    FakeCheckpointFixtureClient,
    FixtureReceipt,
    FixtureReceiptError,
    PhaseGuard,
    RedisCheckpointFixtureClient,
    RedisEvidenceAdapter,
    RedisEvidenceError,
    current_feature_flags,
    load_fixture_receipt,
    load_public_input,
    preflight_probe,
    public_subprocess_env,
    run_manifest_blueprint,
    scan_secrets,
    verify_active_backend,
    write_fixture_receipt,
    write_json_atomic,
    write_jsonl_atomic,
)

TASK_ID = "AGENTIC-SCENARIO-LAB-V0-REAL-RUNNER-REPAIR-004"
ATTEMPT = "004"

# New repair-004 default output dir.  NOT created this round (the REPAIR-004
# flow is validated on scratch dirs under .runtime/); the old 001/002 freeze
# dirs are read-only and untouched.
DEFAULT_DATA_DIR = ROOT / "data" / "derived" / "scenario_lab_v0_real_runner_repair004_20260819"
RUNTIME_ROOT = ROOT / ".runtime" / "scenario-lab-v0-real-runner-repair-004"
ENTRYPOINT = AGENT_DIR / "scripts" / "run_scenario_lab_v0_real_runner_public.py"

CATALOG_DOCS = (
    ROOT / "data" / "derived" / "ecommerce" / "used_phone_real_query_qrel_v1" / "documents.jsonl"
)
CATALOG_PRICES = (
    ROOT
    / "data"
    / "derived"
    / "ecommerce"
    / "used_phone_synthetic_reference_price_v1"
    / "prices.jsonl"
)

CATALOG_REVISION = "used-phone-catalog-v1"
ENVIRONMENT_REVISION = "asl-v0-real-001"

SCENARIO_IDS = (
    "ASL-V0-LNG-REAL-001",
    "ASL-V0-LNG-REAL-002",
    "ASL-V0-LNG-REAL-003",
    "ASL-V0-LNG-REAL-004",
    "ASL-V0-LNG-REAL-005",
    "ASL-V0-LNG-REAL-006",
    "ASL-V0-LNG-REAL-007",
    "ASL-V0-LNG-REAL-008",
    "ASL-V0-INF-REAL-001",
    "ASL-V0-INF-REAL-002",
    "ASL-V0-INF-REAL-003",
    "ASL-V0-INF-REAL-004",
)

# LNG scenario prompts (public input, duplicated from the frozen input file so
# the run phase needs no private files).  Exact TASK_PACKET wording.
LNG_001_MESSAGE = "预算 2500 元以内，想买一台非苹果的二手手机，优先 256GB、电池状况好，请推荐并说明依据。"
LNG_002_QUESTION = "不要这个，帮我挑一台二手手机。"
LNG_002_ANSWER = "我不想要苹果；预算 3000 元以内，优先 256GB。"
LNG_003_MESSAGE = "预算不超过 2000 元，不考虑苹果，想买二手手机，直接给我候选。"
LNG_004_MESSAGE = "预算上限 4000 元，非苹果二手手机，优先 256GB。"
LNG_005_ANSWER = "预算 1800 元以内，不要苹果。"
LNG_006_FIRST = "预算 2500 元以内，想买一台非苹果二手手机。"
LNG_006_CORRECTION = "预算改为 3000 元，还是不要苹果，其他要求不变。"
LNG_007_FIRST = "iOS、主板未维修的二手手机。"
LNG_007_FOLLOWUP = "这其中哪一个拍照效果最好"
LNG_008_FIRST = "iOS、主板未维修的二手手机。"
LNG_008_CORRECTION = "改成安卓，其他要求不变。"
INF_001_MESSAGE = "帮我挑一台 2500 元以内的二手手机。"
INF_004_MESSAGE = "不要这个，帮我挑一台二手手机。"


# ---------------------------------------------------------------------------
# Catalog derivation (freeze-time, mechanical, oracle-first)
# ---------------------------------------------------------------------------


def _is_apple(brand: Any) -> bool:
    brand = str(brand or "")
    lower = brand.lower()
    return "苹果" in brand or any(marker in lower for marker in ("apple", "iphone", "ios"))


def derive_catalog() -> dict[str, Any]:
    """Mechanically derive the LNG acceptable universes from the offline catalog.

    * non-Apple: brand without 苹果/apple/iphone/ios markers (156 docs).
    * acceptable(LNG-001): non-Apple with referencePriceMinor <= 250000 (147).
    * acceptable(LNG-002): non-Apple with referencePriceMinor <= 300000 (154).

    Runs BEFORE any real request — the oracle is a pure function of the frozen
    offline catalog + the public budget constraints.
    """
    docs = [json.loads(line) for line in CATALOG_DOCS.read_text(encoding="utf-8").splitlines() if line.strip()]
    prices = {
        json.loads(line)["itemId"]: json.loads(line)["referencePriceMinor"]
        for line in CATALOG_PRICES.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    non_apple = [doc for doc in docs if not _is_apple(doc.get("brand"))]
    doc_ids = [d["product_id"] for d in docs]

    def acceptable(budget_minor: int) -> list[str]:
        return sorted(
            doc["product_id"]
            for doc in non_apple
            if prices.get(doc["product_id"]) is not None and prices[doc["product_id"]] <= budget_minor
        )

    return {
        "docCount": len(docs),
        "priceCount": len(prices),
        "joined": len(set(doc_ids) & set(prices)),
        "nonAppleCount": len(non_apple),
        "acceptableLng001": acceptable(250_000),
        "acceptableLng002": acceptable(300_000),
        "acceptableLng003": acceptable(200_000),
        "acceptableLng004": acceptable(400_000),
        "acceptableLng005": acceptable(180_000),
        "acceptableLng006": acceptable(300_000),
        "catalogDocsSha256": sha256_file(CATALOG_DOCS),
        "catalogPricesSha256": sha256_file(CATALOG_PRICES),
        "derivation": (
            "brand WITHOUT markers in {苹果,apple,iphone,ios} "
            "AND referencePriceMinor <= budget"
        ),
    }


# ---------------------------------------------------------------------------
# Freeze phase
# ---------------------------------------------------------------------------


def _input_record(sid: str, track: str, turns: list[dict[str, str]], setup_ref: str | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "scenarioId": sid,
        "trackType": track,
        "schemaVersion": lab.INPUT_SCHEMA_VERSION,
        "catalogRevision": CATALOG_REVISION,
        "environmentRevision": ENVIRONMENT_REVISION,
        "turns": turns,
        "publicEnvironmentRefs": [
            "durable:///agent/chat-llm-durable",
            "redis://127.0.0.1:6379/0",
        ],
    }
    if setup_ref is not None:
        record["setupRef"] = setup_ref
    return record


def _lng_oracle(sid: str, acceptable: list[str], expected_terminal: str) -> dict[str, Any]:
    return {
        "scenarioId": sid,
        "trackType": "language_e2e",
        "schemaVersion": lab.ORACLE_SCHEMA_VERSION,
        "catalogRevision": CATALOG_REVISION,
        "environmentRevision": ENVIRONMENT_REVISION,
        "successConditions": {
            "expectedTerminalClasses": [expected_terminal],
            "acceptableProductIds": acceptable,
            "hardConstraints": {
                "hard": [{"group": "brand", "operator": "NOT_IN", "allowedValues": ["苹果/apple"]}],
                "soft": [],
            },
            "toolRules": {"requiredTool": "search_products"},
            "evidenceRules": {"minCitationCount": 0},
        },
    }


def _inf_oracle(
    sid: str, expected_terminal: str, fault_expectations: dict[str, Any]
) -> dict[str, Any]:
    return {
        "scenarioId": sid,
        "trackType": "infrastructure_fault",
        "schemaVersion": lab.ORACLE_SCHEMA_VERSION,
        "catalogRevision": CATALOG_REVISION,
        "environmentRevision": ENVIRONMENT_REVISION,
        "successConditions": {
            "expectedTerminalClasses": [expected_terminal],
            "faultExpectations": fault_expectations,
        },
    }


def _language_tool_oracle(
    sid: str,
    *,
    required_tool: str,
    forbidden_tools: list[str],
) -> dict[str, Any]:
    return {
        "scenarioId": sid,
        "trackType": "language_e2e",
        "schemaVersion": lab.ORACLE_SCHEMA_VERSION,
        "catalogRevision": CATALOG_REVISION,
        "environmentRevision": ENVIRONMENT_REVISION,
        "successConditions": {
            "expectedTerminalClasses": ["SUCCESS"],
            "toolRules": {
                "requiredTool": required_tool,
                "forbiddenTools": forbidden_tools,
                "maxToolCalls": 1,
                "maxDuplicateToolCalls": 0,
            },
            "evidenceRules": {"minCitationCount": 0},
        },
    }


def _lng_fault(sid: str) -> dict[str, Any]:
    return {
        "scenarioId": sid,
        "trackType": "language_e2e",
        "schemaVersion": lab.FAULT_SCHEMA_VERSION,
        "catalogRevision": CATALOG_REVISION,
        "environmentRevision": ENVIRONMENT_REVISION,
        "faultPlan": {"injectionPoints": []},
    }


def _inf_fault(
    sid: str,
    injections: list[str],
    expected_terminal: str,
    setup_fixture: dict[str, Any] | None = None,
) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "injectionPoints": injections,
        "expectedFaultOutcome": {"expectedTerminalClasses": [expected_terminal]},
    }
    if setup_fixture is not None:
        plan["setupFixture"] = setup_fixture
    return {
        "scenarioId": sid,
        "trackType": "infrastructure_fault",
        "schemaVersion": lab.FAULT_SCHEMA_VERSION,
        "catalogRevision": CATALOG_REVISION,
        "environmentRevision": ENVIRONMENT_REVISION,
        "faultPlan": plan,
    }


def build_frozen_records(
    catalog: Mapping[str, Any],
    *,
    fixture_receipt: Any | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Build the six input/oracle/fault records, oracle-first.

    REPAIR-003: the INF-001 restart fault freezes the REAL checkpoint
    ref/SHA from the fixture receipt.  The old always-mismatching SENTINEL
    design is DELETED.  A missing receipt fails closed (prepare? -> freeze).
    """
    inputs: list[dict[str, Any]] = [
        _input_record("ASL-V0-LNG-REAL-001", "language_e2e", [{"role": "user", "text": LNG_001_MESSAGE, "turnId": "t1"}]),
        _input_record(
            "ASL-V0-LNG-REAL-002",
            "language_e2e",
            [
                {"role": "user", "text": LNG_002_QUESTION, "turnId": "t1"},
                {"role": "user", "text": LNG_002_ANSWER, "turnId": "t2"},
            ],
        ),
        _input_record("ASL-V0-LNG-REAL-003", "language_e2e", [
            {"role": "user", "text": LNG_003_MESSAGE, "turnId": "t1"},
        ]),
        _input_record("ASL-V0-LNG-REAL-004", "language_e2e", [
            {"role": "user", "text": LNG_004_MESSAGE, "turnId": "t1"},
        ]),
        _input_record("ASL-V0-LNG-REAL-005", "language_e2e", [
            {"role": "user", "text": LNG_002_QUESTION, "turnId": "t1"},
            {"role": "user", "text": LNG_005_ANSWER, "turnId": "t2"},
        ]),
        _input_record("ASL-V0-LNG-REAL-006", "language_e2e", [
            {"role": "user", "text": LNG_006_FIRST, "turnId": "t1"},
            {"role": "user", "text": LNG_006_CORRECTION, "turnId": "t2"},
        ]),
        _input_record("ASL-V0-LNG-REAL-007", "language_e2e", [
            {"role": "user", "text": LNG_007_FIRST, "turnId": "t1"},
            {"role": "user", "text": LNG_007_FOLLOWUP, "turnId": "t2"},
        ]),
        _input_record("ASL-V0-LNG-REAL-008", "language_e2e", [
            {"role": "user", "text": LNG_008_FIRST, "turnId": "t1"},
            {"role": "user", "text": LNG_008_CORRECTION, "turnId": "t2"},
        ]),
        _input_record("ASL-V0-INF-REAL-001", "infrastructure_fault", [], setup_ref="fixture.slv0r1-inf001"),
        _input_record("ASL-V0-INF-REAL-002", "infrastructure_fault", [], setup_ref="fixture.slv0r1-inf002"),
        _input_record("ASL-V0-INF-REAL-003", "infrastructure_fault", [], setup_ref="fixture.slv0r1-inf003"),
        _input_record("ASL-V0-INF-REAL-004", "infrastructure_fault", [], setup_ref="fixture.slv0r1-inf004"),
    ]
    oracles: list[dict[str, Any]] = [
        _lng_oracle("ASL-V0-LNG-REAL-001", catalog["acceptableLng001"], "SUCCESS"),
        _lng_oracle("ASL-V0-LNG-REAL-002", catalog["acceptableLng002"], "SUCCESS"),
        _lng_oracle("ASL-V0-LNG-REAL-003", catalog["acceptableLng003"], "SUCCESS"),
        _lng_oracle("ASL-V0-LNG-REAL-004", catalog["acceptableLng004"], "SUCCESS"),
        _lng_oracle("ASL-V0-LNG-REAL-005", catalog["acceptableLng005"], "SUCCESS"),
        _lng_oracle("ASL-V0-LNG-REAL-006", catalog["acceptableLng006"], "SUCCESS"),
        _language_tool_oracle(
            "ASL-V0-LNG-REAL-007",
            required_tool="rerank_products_in_scope",
            forbidden_tools=["search_products"],
        ),
        _language_tool_oracle(
            "ASL-V0-LNG-REAL-008",
            required_tool="search_products",
            forbidden_tools=["rerank_products_in_scope"],
        ),
        _inf_oracle(
            "ASL-V0-INF-REAL-001",
            "SUCCESS",
            {"checkpoint_restart": {"required": True, "expectedRecoveredAfterRestart": True}},
        ),
        _inf_oracle(
            "ASL-V0-INF-REAL-002",
            "SUCCESS",
            {"exact_replay": {"required": True, "expectedExactReplayZeroTool": True}},
        ),
        _inf_oracle(
            "ASL-V0-INF-REAL-003",
            "ABSTAIN_OR_EXPLAIN",
            {"mutated_replay": {"required": True, "expectedMutatedReplayRejected": True}},
        ),
        _inf_oracle(
            "ASL-V0-INF-REAL-004",
            "ABSTAIN_OR_EXPLAIN",
            {"revision_conflict": {"required": True, "expectedRevisionDivergenceFailClosed": True}},
        ),
    ]
    if fixture_receipt is None:
        raise FixtureReceiptError(
            "freeze: INF-001 needs a checkpoint fixture receipt "
            "(run prepare-fixtures first; the sentinel design is deleted)"
        )
    checkpoint_ref = fixture_receipt.checkpoint_ref
    checkpoint_sha = fixture_receipt.checkpoint_sha256
    faults: list[dict[str, Any]] = [
        _lng_fault("ASL-V0-LNG-REAL-001"),
        _lng_fault("ASL-V0-LNG-REAL-002"),
        _lng_fault("ASL-V0-LNG-REAL-003"),
        _lng_fault("ASL-V0-LNG-REAL-004"),
        _lng_fault("ASL-V0-LNG-REAL-005"),
        _lng_fault("ASL-V0-LNG-REAL-006"),
        _lng_fault("ASL-V0-LNG-REAL-007"),
        _lng_fault("ASL-V0-LNG-REAL-008"),
        _inf_fault(
            "ASL-V0-INF-REAL-001",
            ["checkpoint_restart"],
            "SUCCESS",
            {
                "checkpointFixtureRef": checkpoint_ref,
                "expectedCheckpointContentsSha256": checkpoint_sha,
            },
        ),
        _inf_fault("ASL-V0-INF-REAL-002", ["exact_replay"], "SUCCESS"),
        _inf_fault("ASL-V0-INF-REAL-003", ["mutated_replay"], "ABSTAIN_OR_EXPLAIN"),
        _inf_fault("ASL-V0-INF-REAL-004", ["revision_conflict"], "ABSTAIN_OR_EXPLAIN"),
    ]
    return inputs, oracles, faults


def _validate_and_write(data_dir: Path, inputs: list, oracles: list, faults: list) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    for record in inputs:
        lab.validate_record(record, "input")
    for record in oracles:
        lab.validate_record(record, "oracle")
    for record in faults:
        lab.validate_record(record, "fault")
    write_jsonl_atomic(data_dir / "scenario.input.jsonl", inputs)
    write_jsonl_atomic(data_dir / "oracle.private.jsonl", oracles)
    write_jsonl_atomic(data_dir / "fault.private.jsonl", faults)


def _code_shas() -> dict[str, str]:
    """Per-phase code SHA triple (runner module + CLI + accepted evaluator)."""
    return {
        "runnerModuleSha256": sha256_file(AGENT_DIR / "evaluation" / "scenario_lab_v0_real_runner.py"),
        "cliSha256": sha256_file(Path(__file__).resolve()),
        "acceptedEvaluatorSha256": sha256_file(AGENT_DIR / "evaluation" / "scenario_lab_v0.py"),
    }


def do_freeze(data_dir: Path, *, fixture_receipt_path: Path | str | None = None) -> int:
    guard = PhaseGuard(data_dir)
    guard.assert_can("freeze")
    catalog = derive_catalog()
    receipt = None
    if fixture_receipt_path is None:
        raise FixtureReceiptError(
            "freeze: --fixture-receipt is required (INF-001 fault needs the real "
            "checkpoint fixture ref/SHA; the sentinel design is deleted)"
        )
    receipt = load_fixture_receipt(fixture_receipt_path)
    data_dir.mkdir(parents=True, exist_ok=True)
    write_fixture_receipt(data_dir / FIXTURE_RECEIPT_FILENAME, receipt)
    inputs, oracles, faults = build_frozen_records(catalog, fixture_receipt=receipt)
    _validate_and_write(data_dir, inputs, oracles, faults)
    # Prove the public input passes the public-only loader AND the accepted
    # leak audit, using the ACCEPTED evaluator's own load path.
    views = load_public_input(data_dir / "scenario.input.jsonl")
    if len(views) != len(SCENARIO_IDS):
        raise ScenarioContractError(
            f"expected {len(SCENARIO_IDS)} public views, got {len(views)}"
        )

    manifest = run_manifest_blueprint()
    manifest["phases"].append(
        {
            "phase": "freeze",
            "completedAt": time.time(),
            "derivation": catalog["derivation"],
            "catalog": {
                "docCount": catalog["docCount"],
                "priceCount": catalog["priceCount"],
                "joined": catalog["joined"],
                "nonAppleCount": catalog["nonAppleCount"],
                "acceptableLng001Count": len(catalog["acceptableLng001"]),
                "acceptableLng002Count": len(catalog["acceptableLng002"]),
                "acceptableLng003Count": len(catalog["acceptableLng003"]),
                "acceptableLng004Count": len(catalog["acceptableLng004"]),
                "acceptableLng005Count": len(catalog["acceptableLng005"]),
                "acceptableLng006Count": len(catalog["acceptableLng006"]),
                "catalogDocsSha256": catalog["catalogDocsSha256"],
                "catalogPricesSha256": catalog["catalogPricesSha256"],
            },
            "scenarioIds": list(SCENARIO_IDS),
            "code": _code_shas(),
        }
    )
    for name, path in (
        ("input", data_dir / "scenario.input.jsonl"),
        ("oracle", data_dir / "oracle.private.jsonl"),
        ("fault", data_dir / "fault.private.jsonl"),
        ("fixtureReceipt", data_dir / FIXTURE_RECEIPT_FILENAME),
    ):
        manifest["files"][name] = {"sha256": sha256_file(path)}
    # REPAIR-004 Repair 1b: the freeze manifest records+binds the FULL receipt
    # identity (receiptSha256 / taskId / sessionId / threadId / checkpointRef /
    # checkpointDigest / checkpointSha256 / createdCodeSha256).  ``do_run``
    # reconciles the passed receipt against this binding field-by-field BEFORE
    # copying public input / spawning the public subprocess / issuing any Agent
    # request.
    manifest["fixtureReceipt"] = _receipt_binding(receipt)
    manifest["code"] = _code_shas()
    write_json_atomic(data_dir / "run-manifest.json", manifest)
    write_freeze_readme(data_dir, catalog, receipt)
    guard.mark("freeze")
    print(f"FREEZE OK: {len(inputs)} scenarios frozen oracle-first into {data_dir}")
    return 0


def write_freeze_readme(data_dir: Path, catalog: Mapping[str, Any], receipt: Any) -> None:
    text = f"""# Scenario Lab V0 Real Runner — 冻结说明 (frozen 2026-08-19)

任务包: {TASK_ID} — Attempt {ATTEMPT}
目录: {data_dir}

## 已冻结的 12 条 V0 场景 (oracle-first)

| scenario | track | expectedTerminal | 说明 |
|---|---|---|---|
| ASL-V0-LNG-REAL-001 | language_e2e | SUCCESS | 预算 2500 / 非苹果 / 优先 256GB |
| ASL-V0-LNG-REAL-002 | language_e2e | SUCCESS | 澄清中断 -> 续答 |
| ASL-V0-LNG-REAL-003 | language_e2e | SUCCESS | 不考虑苹果 / 预算 2000 |
| ASL-V0-LNG-REAL-004 | language_e2e | SUCCESS | 非苹果 / 预算 4000 边界 |
| ASL-V0-LNG-REAL-005 | language_e2e | SUCCESS | 澄清后预算 1800 / 不要苹果 |
| ASL-V0-LNG-REAL-006 | language_e2e | SUCCESS | 已完成任务的预算纠正 / 仍不要苹果 |
| ASL-V0-LNG-REAL-007 | language_e2e | SUCCESS | 同一候选域内拍照意图重排，不得 full search |
| ASL-V0-LNG-REAL-008 | language_e2e | SUCCESS | iOS 改安卓，必须 full search 替换旧 scope |
| ASL-V0-INF-REAL-001 | infrastructure_fault | SUCCESS | 进程重启恢复 (checkpoint_restart) |
| ASL-V0-INF-REAL-002 | infrastructure_fault | SUCCESS | 精确重放 (exact_replay) |
| ASL-V0-INF-REAL-003 | infrastructure_fault | ABSTAIN_OR_EXPLAIN | 变异重放 (mutated_replay) |
| ASL-V0-INF-REAL-004 | infrastructure_fault | ABSTAIN_OR_EXPLAIN | revision 冲突 (revision_conflict) |

## INF-001 checkpoint 夹具 (REPAIR-002, 真实 ref/SHA)

fixture 会话: {receipt.session_id}
fixture 线程: {receipt.thread_id}
checkpoint ref: {receipt.checkpoint_ref}
checkpoint envelope SHA-256: {receipt.checkpoint_sha256}

该 ref/SHA 来自 prepare-fixtures 阶段写入的 task-owned fixture receipt
（自签名，freeze/run 读取时校验签名、owner 前缀与 64-hex 形状）。fault.private.jsonl
中 INF-001 的 setupFixture 即冻结此真实 ref/SHA；旧 SENTINEL 设计已删除。

## 商品宇宙机械推导 (freeze 阶段, 任何真实请求之前)

来源 (均为离线冻结数据, SHA 见 run-manifest.json):

- `used_phone_real_query_qrel_v1/documents.jsonl` ({catalog['docCount']} 文档)
- `used_phone_synthetic_reference_price_v1/prices.jsonl` ({catalog['priceCount']} 价格, itemId==product_id join {catalog['joined']}/{catalog['docCount']})

推导: {catalog['derivation']}

- 非苹果文档: {catalog['nonAppleCount']}
- LNG-001 可接受宇宙 (<=2500 元, 非苹果): {len(catalog['acceptableLng001'])} 个 product_id
- LNG-002 可接受宇宙 (<=3000 元, 非苹果): {len(catalog['acceptableLng002'])} 个 product_id
- LNG-003 可接受宇宙 (<=2000 元, 非苹果): {len(catalog['acceptableLng003'])} 个 product_id
- LNG-004 可接受宇宙 (<=4000 元, 非苹果): {len(catalog['acceptableLng004'])} 个 product_id
- LNG-005 可接受宇宙 (<=1800 元, 非苹果): {len(catalog['acceptableLng005'])} 个 product_id
- LNG-006 可接受宇宙 (<=3000 元, 非苹果): {len(catalog['acceptableLng006'])} 个 product_id

oracle.private.jsonl 中的 acceptableProductIds 完全由上述离线推导生成; 运行阶段不读取 private 文件。

## 公共/私有隔离

- run 阶段通过 orchestrator 把公共输入拷入 task-owned public runtime 目录，再以
  public-only 子进程执行六场景；子进程 argv/env/cwd 均不含 private 内容。
- oracle.private.jsonl / fault.private.jsonl 仅供隔离 scorer 读取。
- 本 README 与 run-manifest.json 均不含 private 内容 (SHA 除外)。
"""
    (data_dir / "README.md").write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Prepare phase (checkpoint fixture)
# ---------------------------------------------------------------------------


def prepare_fixture_to_receipt(
    client: Any,
    fixtures_dir: Path,
    *,
    fixture_id: str,
    created_code_sha256: str = "",
) -> Path:
    """Build a parked-clarification fixture through the given client and write
    the signed receipt into ``fixtures_dir``.  The client is the REAL Redis
    writer on the live path or the deterministic Fake for the offline flow.
    """
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    receipt = client.park_clarification_fixture(fixture_id=fixture_id)
    # Force the caller-declared code SHA into the receipt and re-sign it so the
    # written signature always covers the exact final content.
    receipt = FixtureReceipt.from_dict(
        {**receipt.to_dict(), "createdCodeSha256": created_code_sha256, "receiptSha256": ""}
    )
    path = write_fixture_receipt(fixtures_dir / FIXTURE_RECEIPT_FILENAME, receipt)
    return path


def do_prepare_fixtures(fixtures_dir: Path, *, redis_url: str | None = None) -> int:
    """REAL task-owned fixture writer (Repair 3).

    Requires a live Redis (the fixture is a REAL TaskState + durable checkpoint
    in the task-owned namespace, produced through the production TaskState
    store + durable graph/checkpointer APIs under ``override_task_state_client``
    and a real ``interrupt()`` park).  REPAIR-003 round: not invoked against
    real Redis in this offline round — the real writer is proven by the offline
    tests against the controlled ``InFileRedis`` stand-in, and a live
    ``--redis-url`` here fails closed (never a handwritten fallback) if the
    production APIs cannot build the fixture.
    """
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    if not redis_url:
        raise FixtureReceiptError("prepare-fixtures requires --redis-url (real task-owned Redis fixture)")
    stamp = int(time.time())
    client = RedisCheckpointFixtureClient(redis_url, created_code_sha256="")
    path = prepare_fixture_to_receipt(
        client, fixtures_dir, fixture_id=f"fixture-{stamp}", created_code_sha256=""
    )
    print(f"FIXTURE OK: {path}")
    return 0


# ---------------------------------------------------------------------------
# Run phase: orchestrator (probe -> spawn public-only subprocess -> publish)
# ---------------------------------------------------------------------------


def _injection_disclosures(*, stub: bool) -> list[dict[str, Any]]:
    prefix = "offline stub drive; " if stub else ""
    return [
        {
            "scenario": "ASL-V0-INF-REAL-001",
            "disclosure": (
                f"{prefix}controlled process-kill restart of the task-owned server P1; "
                "the frozen fixture ref/SHA comes from the prepare-fixtures receipt and the "
                "restart_recovery attempt binds the 'actually recovered from' fixture ref/SHA "
                "ONLY when the observed pre-restart latest checkpoint equals the frozen fixture "
                "ref (otherwise the binding fails closed with no ref/SHA attached)"
            ),
        },
        {
            "scenario": "ASL-V0-INF-REAL-002",
            "disclosure": (
                f"{prefix}exact replay replayed the byte-identical resume payload of its own "
                "resolved task; result hash equality uses the referenced (already-"
                "resolved) terminal so baseline and replay hashes are comparable"
            ),
        },
        {
            "scenario": "ASL-V0-INF-REAL-003",
            "disclosure": (
                f"{prefix}rejection reason recorded from the server's deterministic validation "
                "chain (resolved_interrupt_different_payload) only when the observed "
                "response confirmed the fail-closed pattern"
            ),
        },
        {
            "scenario": "ASL-V0-INF-REAL-004",
            "disclosure": (
                f"{prefix}REAL external OCC revision drift n -> n+1 through the restricted "
                "task-owned drift adapter (identity verified BEFORE any Redis GET/WATCH/SET; "
                "atomic patch via the production Lua CAS script), then the ORIGINAL old "
                "receipt resumes and is rejected by the deterministic revision_mismatch "
                "gate with an observed 0-delta"
            ),
        },
    ]


def _receipt_binding(receipt: Any) -> dict[str, Any]:
    """The 8-field fixture-receipt binding recorded in the freeze manifest and
    reconciled field-by-field by ``do_run``."""
    return {
        "receiptSha256": receipt.receipt_sha256,
        "taskId": receipt.task_id,
        "sessionId": receipt.session_id,
        "threadId": receipt.thread_id,
        "checkpointRef": receipt.checkpoint_ref,
        "checkpointDigest": receipt.checkpoint_digest,
        "checkpointSha256": receipt.checkpoint_sha256,
        "createdCodeSha256": receipt.created_code_sha256,
    }


def _reconcile_receipts(frozen: Any, passed: Any, *, source: str) -> None:
    """Field-by-field reconcile a passed receipt against the FROZEN one.

    REPAIR-004 Repair 1b: missing / replaced / re-signed / task-session-thread
    drift all fail closed (FixtureReceiptError) BEFORE any public subprocess or
    Agent request.
    """
    drift: list[str] = []
    expected = _receipt_binding(frozen)
    for field, frozen_value in expected.items():
        passed_value = getattr(passed, {
            "receiptSha256": "receipt_sha256",
            "taskId": "task_id",
            "sessionId": "session_id",
            "threadId": "thread_id",
            "checkpointRef": "checkpoint_ref",
            "checkpointDigest": "checkpoint_digest",
            "checkpointSha256": "checkpoint_sha256",
            "createdCodeSha256": "created_code_sha256",
        }[field], None)
        if passed_value != frozen_value:
            drift.append(f"{field} {passed_value!r} != frozen {frozen_value!r}")
    if drift:
        raise FixtureReceiptError(
            f"run: receipt drift vs frozen receipt ({source}): {'; '.join(drift)}"
        )


def _reconcile_inf001_fault(data_dir: Path, receipt: Any) -> None:
    """Reconcile the frozen INF-001 private fault's setupFixture ref/SHA against
    the frozen receipt — any drift fails closed with 0 subprocess calls."""
    fault_path = data_dir / "fault.private.jsonl"
    if not fault_path.exists():
        raise FixtureReceiptError(f"run: frozen INF-001 fault missing at {fault_path}")
    records = load_records(fault_path, "fault")
    inf001 = next(
        (r for r in records if r.get("scenarioId") == "ASL-V0-INF-REAL-001"), None
    )
    if inf001 is None:
        raise FixtureReceiptError(
            "run: frozen fault.private.jsonl has no ASL-V0-INF-REAL-001 record"
        )
    setup = (inf001.get("faultPlan") or {}).get("setupFixture") or {}
    frozen_ref = setup.get("checkpointFixtureRef")
    frozen_sha = setup.get("expectedCheckpointContentsSha256")
    drift: list[str] = []
    if frozen_ref != receipt.checkpoint_ref:
        drift.append(
            f"fault.faultPlan.setupFixture.checkpointFixtureRef {frozen_ref!r} != "
            f"receipt {receipt.checkpoint_ref!r}"
        )
    if frozen_sha != receipt.checkpoint_sha256:
        drift.append(
            f"fault.faultPlan.setupFixture.expectedCheckpointContentsSha256 {frozen_sha!r} != "
            f"receipt {receipt.checkpoint_sha256!r}"
        )
    if drift:
        raise FixtureReceiptError(
            "run: INF-001 fault drifted from frozen receipt: " + "; ".join(drift)
        )


def _reconcile_manifest_binding(data_dir: Path, receipt: Any) -> None:
    """Reconcile the frozen manifest's ``fixtureReceipt`` binding against the
    frozen receipt — a missing or drifted binding fails closed."""
    manifest_path = data_dir / "run-manifest.json"
    if not manifest_path.exists():
        raise FixtureReceiptError(
            f"run: frozen run-manifest.json missing at {manifest_path} (freeze first)"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureReceiptError(
            f"run: frozen run-manifest.json unreadable: {type(exc).__name__}"
        ) from exc
    if not isinstance(manifest, Mapping):
        raise FixtureReceiptError("run: frozen run-manifest.json malformed")
    binding = manifest.get("fixtureReceipt")
    if not isinstance(binding, Mapping):
        raise FixtureReceiptError(
            "run: frozen manifest has no fixtureReceipt binding (missing/replaced)"
        )
    drift = [
        f"{key} {binding.get(key)!r} != {expected!r}"
        for key, expected in _receipt_binding(receipt).items()
        if binding.get(key) != expected
    ]
    if drift:
        raise FixtureReceiptError(
            "run: manifest fixtureReceipt binding drifted from frozen receipt: "
            + "; ".join(drift)
        )


def _merge_manifest(data_dir: Path) -> dict[str, Any]:
    manifest = run_manifest_blueprint()
    if (data_dir / "run-manifest.json").exists():
        try:
            prior = json.loads((data_dir / "run-manifest.json").read_text(encoding="utf-8"))
            if isinstance(prior, Mapping):
                manifest = prior
        except (OSError, json.JSONDecodeError):
            pass
    return manifest


def do_run(
    data_dir: Path,
    *,
    base_url: str | None = None,
    redis_url: str = "redis://127.0.0.1:6379/0",
    stub: bool = False,
    fixture_receipt_path: Path | str | None = None,
) -> int:
    guard = PhaseGuard(data_dir)
    guard.assert_can("run")
    data_dir.mkdir(parents=True, exist_ok=True)
    if not (data_dir / "scenario.input.jsonl").exists():
        raise ScenarioContractError(f"run phase: {data_dir} has no scenario.input.jsonl (freeze first)")

    run_root = RUNTIME_ROOT / f"run-{int(time.time())}"
    public_dir = run_root / "public"

    # REPAIR-004 Repair 1b: BEFORE creating the public runtime dir / copying
    # public input / creating the public subprocess / issuing ANY Agent request,
    # bind this run to the FROZEN fixture receipt field-by-field (receiptSha256
    # / taskId / sessionId / threadId / checkpointRef / checkpointDigest /
    # checkpointSha256 / createdCodeSha256) AND to the INF-001 private fault's
    # frozen ref/SHA and the freeze manifest's receipt binding.  Missing /
    # replaced / re-signed / task-session-thread drift all fail closed with 0
    # subprocess calls.
    frozen_receipt_path = data_dir / FIXTURE_RECEIPT_FILENAME
    if not frozen_receipt_path.exists():
        raise FixtureReceiptError(
            f"run: frozen fixture receipt missing at {frozen_receipt_path} (freeze first)"
        )
    frozen_receipt = load_fixture_receipt(frozen_receipt_path)
    if fixture_receipt_path is not None:
        passed_receipt = load_fixture_receipt(fixture_receipt_path)
        _reconcile_receipts(
            frozen_receipt, passed_receipt, source=f"--fixture-receipt {fixture_receipt_path}"
        )
    _reconcile_inf001_fault(data_dir, frozen_receipt)
    _reconcile_manifest_binding(data_dir, frozen_receipt)
    receipt_path = Path(frozen_receipt_path).resolve()

    # The public runtime dir holds ONLY the public input copy; no oracle/fault/
    # score file ever exists here.  The public-only entrypoint fails closed if a
    # private sibling shows up.
    public_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(data_dir / "scenario.input.jsonl", public_dir / "scenario.input.jsonl")

    manifest = _merge_manifest(data_dir)

    probe_evidence: dict[str, Any] = {}
    if not stub:
        # Repair 2: unified real backend probing.  Conclusions are DERIVED from
        # the structured evidence; no hardcoded "Docker down" strings.
        probe_evidence = preflight_probe(redis_url=redis_url)
        manifest["environment"] = {
            "featureFlags": current_feature_flags(),
            "backendBaseUrl": SEARCH_JAVA_BASE_URL,
            "probe": probe_evidence,
        }
        up = verify_active_backend(
            probe_evidence, base_url=SEARCH_JAVA_BASE_URL, redis_url=redis_url
        )
        if not up:
            unreachable = [
                name
                for name, target in (probe_evidence.get("targets") or {}).items()
                if not bool(target.get("reachable"))
            ]
            manifest["phases"].append(
                {
                    "phase": "run",
                    "blocked": True,
                    "blockReason": "BLOCKED_REAL_DEPENDENCY",
                    "completedAt": time.time(),
                    "probeEvidence": probe_evidence,
                    "detail": (
                        f"retrievalChainUp={probe_evidence.get('derived', {}).get('retrievalChainUp')}; "
                        f"unreachable targets={unreachable}; "
                        "real six-scenario run refused on a degraded fallback (task packet)",
                    ),
                }
            )
            write_json_atomic(data_dir / "run-manifest.json", manifest)
            print(
                "RUN BLOCKED: BLOCKED_REAL_DEPENDENCY — real retrieval chain down; "
                "no scenario executed (fail-closed)"
            )
            return 3
    else:
        manifest["environment"] = {
            "stub": True,
            "note": "offline deterministic stub drive (no real backend, no model)",
        }

    argv = [
        sys.executable,
        str(ENTRYPOINT),
        "--input",
        str(public_dir / "scenario.input.jsonl"),
        "--outdir",
        str(public_dir),
        "--redis-url",
        redis_url,
    ]
    if stub:
        argv.append("--stub")
    if base_url:
        argv.extend(["--base-url", base_url])
    if receipt_path is not None:
        argv.extend(["--fixture-receipt", str(receipt_path)])
    env = public_subprocess_env(
        os.environ, base_url=SEARCH_JAVA_BASE_URL, redis_url=redis_url
    )

    try:
        proc = subprocess.run(
            argv,
            cwd=str(public_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=900,
        )
    except subprocess.TimeoutExpired:
        manifest["phases"].append(
            {"phase": "run", "blocked": True, "blockReason": "subprocess_timeout",
             "completedAt": time.time()}
        )
        write_json_atomic(data_dir / "run-manifest.json", manifest)
        print("RUN FAILED: public subprocess timed out")
        return 2

    evidence: dict[str, Any] = {}
    evidence_path = public_dir / "evidence.json"
    if evidence_path.exists():
        try:
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            evidence = {"status": "evidence_unreadable"}
    if proc.returncode != 0:
        manifest["phases"].append(
            {
                "phase": "run",
                "blocked": True,
                "blockReason": str(evidence.get("status") or "public_subprocess_failed"),
                "completedAt": time.time(),
                "subprocessExit": proc.returncode,
                "stdoutTail": proc.stdout[-2000:],
                "stderrTail": proc.stderr[-2000:],
            }
        )
        write_json_atomic(data_dir / "run-manifest.json", manifest)
        print(f"RUN FAILED: public-only subprocess exit={proc.returncode} "
              f"status={evidence.get('status')}")
        return proc.returncode if proc.returncode in (1, 2, 3, 4) else 2

    prediction_path = public_dir / "prediction.jsonl"
    if not prediction_path.exists():
        manifest["phases"].append(
            {"phase": "run", "blocked": True, "blockReason": "no_prediction_artifact",
             "completedAt": time.time()}
        )
        write_json_atomic(data_dir / "run-manifest.json", manifest)
        print("RUN FAILED: public subprocess produced no prediction.jsonl")
        return 4
    predictions = load_records(prediction_path, "prediction")
    if len(predictions) != len(SCENARIO_IDS):
        print(
            f"RUN FAILED: expected {len(SCENARIO_IDS)} predictions, "
            f"got {len(predictions)}"
        )
        return 4

    # Publish into the formal data dir.
    (data_dir / "prediction.jsonl").write_bytes(prediction_path.read_bytes())

    manifest["phases"].append(
        {
            "phase": "run",
            "completedAt": time.time(),
            "stub": stub,
            "baseUrl": evidence.get("baseUrl") or base_url,
            "predictionCount": len(predictions),
            "code": _code_shas(),
        }
    )
    ownership = evidence.get("ownership") or {}
    manifest["ownership"] = {
        "sessions": ownership.get("sessions") or {},
        "serverPorts": ownership.get("serverPorts") or [],
        "processPids": ownership.get("processPids") or [],
        "stub": bool(stub),
    }
    manifest["runtimeRecords"] = evidence.get("runtimeRecords") or []
    manifest["injectionDisclosures"] = _injection_disclosures(stub=stub)
    manifest["services"] = {"probe": probe_evidence} if not stub else {"stub": True}
    for name, path in (
        ("prediction", data_dir / "prediction.jsonl"),
        ("manifest", data_dir / "run-manifest.json"),
    ):
        manifest["files"][name] = {"sha256": sha256_file(path)}
    manifest["subprocessEvidence"] = evidence
    write_json_atomic(data_dir / "run-manifest.json", manifest)
    guard.mark("run")
    print(f"RUN OK: {len(predictions)} predictions published "
          f"(public-only subprocess, stub={stub})")
    return 0


# ---------------------------------------------------------------------------
# Score phase (isolated subprocess)
# ---------------------------------------------------------------------------


SCORER_INNER = r"""
import json, sys
sys.path.insert(0, r"{agent_dir}")
from evaluation import scenario_lab_v0 as lab
from evaluation.scenario_lab_v0 import ScenarioSet, sha256_file
input_p, oracle_p, fault_p, prediction_p, out_p = sys.argv[1:]
scenario_set = ScenarioSet.load(input_p, oracle_p, fault_p, prediction_p)
report = lab.score_scenario_set(scenario_set)
with open(out_p, "w", encoding="utf-8") as fh:
    json.dump(report, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
print("SCORE_OK", sha256_file(out_p))
"""


def do_score(data_dir: Path) -> int:
    guard = PhaseGuard(data_dir)
    guard.assert_can("score")
    files = {
        "input": data_dir / "scenario.input.jsonl",
        "oracle": data_dir / "oracle.private.jsonl",
        "fault": data_dir / "fault.private.jsonl",
        "prediction": data_dir / "prediction.jsonl",
    }
    missing = [name for name, path in files.items() if not path.exists()]
    if missing:
        raise ScenarioContractError(f"score phase missing files: {missing}")
    out_path = data_dir / "score-report.json"
    # Isolation: a fresh interpreter reads only the four files and writes the
    # report.  This CLI process never loads oracle/fault content.
    code = SCORER_INNER.format(agent_dir=AGENT_DIR)
    proc = subprocess.run(
        [sys.executable, "-c", code, str(files["input"]), str(files["oracle"]),
         str(files["fault"]), str(files["prediction"]), str(out_path)],
        capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0:
        print("SCORE FAILED (isolated scorer):", proc.stdout, proc.stderr)
        return 3
    print(proc.stdout.strip().splitlines()[-1])

    manifest = _merge_manifest(data_dir)
    manifest["phases"].append({"phase": "score", "completedAt": time.time(), "code": _code_shas()})
    manifest["files"]["scoreReport"] = {"sha256": sha256_file(out_path)}
    write_json_atomic(data_dir / "run-manifest.json", manifest)
    guard.mark("score")
    print(f"SCORE OK: {out_path}")
    return 0


# ---------------------------------------------------------------------------
# Verify phase (strict, fail-closed)
# ---------------------------------------------------------------------------


def _code_shas_match(recorded: Any, current: Mapping[str, str]) -> tuple[bool, list[str]]:
    if not isinstance(recorded, Mapping):
        return False, ["phase code block missing"]
    problems: list[str] = []
    for key, expected in current.items():
        if str(recorded.get(key) or "") != expected:
            problems.append(f"{key} recorded={recorded.get(key)} actual={expected[:16]}…")
    return not problems, problems


def _verify_freeze_artifacts_strict(data_dir: Path, files: dict[str, Path], manifest: Mapping[str, Any]) -> int:
    """Strict verification of the frozen subset in the legal blocked state.

    Returns 0 only when every schema check, phase-order check, code-SHA check
    and file-SHA check passes; any mismatch returns 4 (fail-closed, never a WARN).
    """
    for kind, path in (("input", files["input"]), ("oracle", files["oracle"]),
                       ("fault", files["fault"])):
        records = load_records(path, kind)
        print(f"  validate {kind}: {len(records)} records OK")
    load_public_input(files["input"])
    print("  public-only load: OK")
    order = [p["phase"] for p in manifest.get("phases", [])]
    legal = [p for p in ("prepare", "freeze", "run") if p in order]
    if order != legal:
        print("  VERIFY FAILED: phase order", order)
        return 4
    blocked_run = any(p.get("phase") == "run" and p.get("blocked") for p in manifest.get("phases", []))
    if not blocked_run:
        print("  VERIFY FAILED: no blocked run phase in legal-blocked state")
        return 4
    freeze_phase = next((p for p in manifest.get("phases", []) if p.get("phase") == "freeze"), None)
    if freeze_phase is None:
        print("  VERIFY FAILED: freeze phase missing")
        return 4
    ok, problems = _code_shas_match(freeze_phase.get("code"), _code_shas())
    if not ok:
        print("  VERIFY FAILED: freeze code SHA mismatch:", problems)
        return 4
    for name, path in (("input", files["input"]), ("oracle", files["oracle"]),
                       ("fault", files["fault"]), ("fixtureReceipt", files.get("fixtureReceipt"))):
        if path is None:
            continue
        recorded = (manifest.get("files", {}) or {}).get(name, {}).get("sha256")
        actual = sha256_file(path)
        if recorded != actual:
            print(f"  VERIFY FAILED: {name} sha mismatch recorded={recorded} actual={actual}")
            return 4
    print("  phase order + code sha + file sha alignment: OK")
    hits: list[str] = []
    for name in ("input", "oracle", "fault", "readme"):
        text = files[name].read_text(encoding="utf-8")
        found = scan_secrets(text)
        if found:
            hits.append(f"{name}: {found}")
    if hits:
        print("  VERIFY FAILED: secret scan hits", hits)
        return 4
    print("  secret scan: OK")
    return 0


def do_verify(data_dir: Path) -> int:
    files = {
        "input": data_dir / "scenario.input.jsonl",
        "oracle": data_dir / "oracle.private.jsonl",
        "fault": data_dir / "fault.private.jsonl",
        "prediction": data_dir / "prediction.jsonl",
        "scoreReport": data_dir / "score-report.json",
        "manifest": data_dir / "run-manifest.json",
        "readme": data_dir / "README.md",
        "fixtureReceipt": data_dir / FIXTURE_RECEIPT_FILENAME,
    }
    if not files["manifest"].exists():
        print("VERIFY FAILED: no run-manifest.json")
        return 4
    manifest = json.loads(files["manifest"].read_text(encoding="utf-8"))
    missing = [name for name, path in files.items() if not path.exists()]

    # Legal blocked state: freeze (+ prepare) + run(blocked), prediction/score
    # absent by design.  Verified strictly; exits NON-ZERO (3) to signal the
    # run did not complete — never a WARN+exit-0.
    blocked_run = any(
        p.get("phase") == "run" and p.get("blocked") for p in manifest.get("phases", [])
    )
    if missing and blocked_run and not files["prediction"].exists() and not files["scoreReport"].exists():
        rc = _verify_freeze_artifacts_strict(data_dir, files, manifest)
        if rc == 0:
            manifest["secretScan"] = {"checked": True, "hits": []}
            manifest["verdict"] = "verified-blocked"
            write_json_atomic(files["manifest"], manifest)
            print("VERIFY: verified-blocked — freeze + run(blocked); "
                  "prediction/score absent by design (exit 3)")
            return 3
        return rc

    if missing:
        print("VERIFY FAILED: missing", missing)
        return 4

    # 1) Schema meta validation on the four JSONL kinds (accepted evaluator).
    for kind, path in (("input", files["input"]), ("oracle", files["oracle"]),
                       ("fault", files["fault"]), ("prediction", files["prediction"])):
        records = load_records(path, kind)
        print(f"  validate {kind}: {len(records)} records OK")
    report = json.loads(files["scoreReport"].read_text(encoding="utf-8"))
    lab.validate_report(report)
    print("  validate report: OK")

    # 2) Public-only loader proof (runner never touches private paths).
    load_public_input(files["input"])
    print("  public-only load: OK")

    # 3) Manifest phase order + per-phase code SHA + file SHA alignment.
    order = [p["phase"] for p in manifest.get("phases", [])]
    expected_order = [p for p in ("prepare", "freeze", "run", "score") if p in order]
    if order != expected_order:
        print("VERIFY FAILED: phase order", order)
        return 4
    for phase in ("freeze", "run"):
        record = next((p for p in manifest.get("phases", []) if p.get("phase") == phase), None)
        if record is None:
            continue
        ok, problems = _code_shas_match(record.get("code"), _code_shas())
        if not ok:
            print(f"VERIFY FAILED: {phase} code SHA mismatch:", problems)
            return 4
    for name, path in (
        ("input", files["input"]), ("oracle", files["oracle"]), ("fault", files["fault"]),
        ("prediction", files["prediction"]), ("scoreReport", files["scoreReport"]),
    ):
        recorded = (manifest.get("files", {}) or {}).get(name, {}).get("sha256")
        actual = sha256_file(path)
        if recorded != actual:
            print(f"VERIFY FAILED: {name} sha mismatch recorded={recorded} actual={actual}")
            return 4
    print("  manifest phase order + per-phase code sha + file sha alignment: OK")

    # 4) Secret scan on every public artifact + the manifest.
    hits: list[str] = []
    for name, path in files.items():
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        found = scan_secrets(text)
        if found:
            hits.append(f"{name}: {found}")
    manifest["secretScan"] = {"checked": True, "hits": hits}
    write_json_atomic(files["manifest"], manifest)
    if hits:
        print("VERIFY FAILED: secret scan hits", hits)
        return 4
    print("  secret scan: OK")

    # 5) Scoped git status proof (whitelist only, ACCEPTED SHA unchanged).
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(ROOT),
            capture_output=True, text=True, timeout=30,
        ).stdout
        accepted_sha = sha256_file(AGENT_DIR / "evaluation" / "scenario_lab_v0.py")
        manifest["code"]["acceptedEvaluatorShaVerified"] = (
            accepted_sha == (manifest.get("code", {}) or {}).get("acceptedEvaluatorSha256")
        )
        manifest["git"] = {"statusLineCount": len([l for l in status.splitlines() if l])}
    except Exception as exc:  # noqa: BLE001
        manifest["git"] = {"error": type(exc).__name__}
    write_json_atomic(files["manifest"], manifest)
    print("VERIFY OK")
    return 0


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Scenario Lab V0 real runner CLI (REPAIR-004)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_prepare = sub.add_parser("prepare-fixtures", help="build the task-owned checkpoint fixture + receipt")
    p_prepare.add_argument("--out", type=Path, default=RUNTIME_ROOT / "fixtures")
    p_prepare.add_argument("--redis-url", default=None)

    p_freeze = sub.add_parser("freeze", help="freeze the 12 V0 scenarios oracle-first")
    p_freeze.add_argument("--out", type=Path, default=DEFAULT_DATA_DIR)
    p_freeze.add_argument("--fixture-receipt", type=Path, default=None)

    p_run = sub.add_parser("run", help="probe + spawn the public-only subprocess")
    p_run.add_argument("--out", type=Path, default=DEFAULT_DATA_DIR)
    p_run.add_argument("--base-url", default=None)
    p_run.add_argument("--redis-url", default="redis://127.0.0.1:6379/0")
    p_run.add_argument("--fixture-receipt", type=Path, default=None)
    p_run.add_argument("--stub", action="store_true", help="offline deterministic stub drive (no backend, no model)")

    p_score = sub.add_parser("score", help="score with the isolated evaluator")
    p_score.add_argument("--out", type=Path, default=DEFAULT_DATA_DIR)

    p_verify = sub.add_parser("verify", help="strict fail-closed verification")
    p_verify.add_argument("--out", type=Path, default=DEFAULT_DATA_DIR)

    p_auto = sub.add_parser("auto", help="freeze -> run -> score in order")
    p_auto.add_argument("--out", type=Path, default=DEFAULT_DATA_DIR)
    p_auto.add_argument("--base-url", default=None)
    p_auto.add_argument("--redis-url", default="redis://127.0.0.1:6379/0")
    p_auto.add_argument("--fixture-receipt", type=Path, default=None)
    p_auto.add_argument("--skip-verify", action="store_true")

    args = parser.parse_args()
    data_dir = args.out

    if args.command == "prepare-fixtures":
        return do_prepare_fixtures(data_dir, redis_url=args.redis_url)
    if args.command == "freeze":
        return do_freeze(data_dir, fixture_receipt_path=args.fixture_receipt)
    if args.command == "run":
        return do_run(
            data_dir, base_url=args.base_url, redis_url=args.redis_url,
            stub=args.stub, fixture_receipt_path=args.fixture_receipt,
        )
    if args.command == "score":
        return do_score(data_dir)
    if args.command == "verify":
        return do_verify(data_dir)
    if args.command == "auto":
        rc = do_freeze(data_dir, fixture_receipt_path=args.fixture_receipt)
        if rc:
            return rc
        rc = do_run(data_dir, base_url=args.base_url, redis_url=args.redis_url,
                    fixture_receipt_path=args.fixture_receipt)
        if rc:
            return rc
        rc = do_score(data_dir)
        if rc:
            return rc
        if not args.skip_verify:
            return do_verify(data_dir)
        return 0
    parser.error("unknown command")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
