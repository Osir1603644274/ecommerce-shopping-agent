import hashlib
import json
from pathlib import Path

from evaluation.used_phone_react_independent_author_package_v1 import (
    EXPECTED_CLASSES,
    validate_submission,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _fixture(root: Path) -> tuple[Path, list[dict], dict]:
    classes = [
        "adaptive_needed",
        "deterministic_control",
        "negative_control",
    ] * 3
    first_turns = [
        "请按颜色与保修状态整理一组候选",
        "我想先查看存储容量明确的可选项",
        "请列出能够核验屏幕状况的候选",
        "先找出支持当面验货的设备",
        "我只考虑能够说明维修历史的条目",
        "请按成色和附件完整度进行初筛",
        "帮我寻找来源信息较完整的手机",
        "我想查看交付时间有明确说明的候选",
        "请先整理电池信息能够核实的选项",
    ]
    second_turns = [
        "随后把保修要求改成仅作为偏好",
        "接着去掉刚才提出的容量限制",
        "然后仅依据已经验证的字段说明差异",
        "现在增加一个不同的交付地点条件",
        "请继续处理但不要推断未知维修情况",
        "接下来排除刚才结果中的某个品牌",
        "然后比较先前范围中指定的两项",
        "现在将时间要求替换成另一日期",
        "最后只展示无需补充证据的已有属性",
    ]
    rows = [
        {
            "scenarioId": f"BLIND-V1-{index:03d}",
            "schemaVersion": "used-phone-react-independent-blind-public-v1",
            "language": "zh-CN",
            "provenanceKind": "independent_blind_author",
            "generalizationClass": classes[index - 1],
            "tags": [f"topic_{index}", f"shape_{index}"],
            "turns": [
                {"turnId": "T1", "text": first_turns[index - 1]},
                {"turnId": "T2", "text": second_turns[index - 1]},
            ],
        }
        for index in range(1, 10)
    ]
    scenarios = root / "public" / "scenarios.jsonl"
    _write_jsonl(scenarios, rows)
    targets = {
        f"{row['scenarioId']}:T2": {"requiredBehavior": f"private-contract-{index}"}
        for index, row in enumerate(rows, 1)
        if row["generalizationClass"] == "adaptive_needed"
    }
    prereg = {
        "schemaVersion": "used-phone-react-independent-blind-preregistration-v1",
        "status": "FROZEN_BEFORE_EXECUTION",
        "frozenDate": "2026-08-26",
        "dataset": "../public/scenarios.jsonl",
        "datasetSha256": hashlib.sha256(scenarios.read_bytes()).hexdigest(),
        "scenarioCount": 9,
        "turnCount": 18,
        "pairedRunCount": 2,
        "classCounts": EXPECTED_CLASSES,
        "applicationSourceFreeze": {"agent/app/example.py": "a" * 64},
        "executionFreeze": {
            "modelConfigurationSha256": "b" * 64,
            "requestTimeoutSeconds": 90,
            "reservedPorts": [28101, 28102],
        },
        "adaptiveTargets": targets,
        "automatedHardGates": {
            "runnerErrors": 0,
            "runtimeMismatches": 0,
            "publishedOptionViolations": 0,
            "sequenceReceiptCompletenessRate": 1.0,
            "reactDecisionCallsOutsideAdaptiveTargets": 0,
            "fixedAndReactAnswerCompletenessRate": 1.0,
        },
        "humanDirectionGate": {
            "minimumDirectFullPacketReviewers": 2,
            "minimumJudgeablePreferenceShare": 0.6,
            "requireNoLowerConstraintFidelityMeanByClass": True,
            "requireNoLowerEvidenceDisciplineMeanByClass": True,
        },
        "freezePolicy": {
            "noApplicationSourceChangesAfterFreeze": True,
            "failedRunsMustBePreserved": True,
            "noScenarioRemovalAfterSeeingOutputs": True,
            "privateOracleHiddenUntilRunsComplete": True,
        },
        "claimBoundary": {
            "canProveStatisticalSignificance": False,
            "canProveGeneralSuperiority": False,
            "canAuthorizeDefaultRuntimeSwitch": False,
        },
    }
    _write_json(root / "private" / "preregistration.json", prereg)
    return scenarios, rows, prereg


def test_accepts_complete_frozen_independent_package(tmp_path: Path) -> None:
    _fixture(tmp_path)

    report = validate_submission(package_root=tmp_path)

    assert report["status"] == "ACCEPT_FROZEN_INTAKE"
    assert report["failureCount"] == 0
    assert report["claimBoundary"]["provesReactSuperiority"] is False


def test_rejects_leaks_placeholders_hash_and_class_imbalance(tmp_path: Path) -> None:
    scenarios, rows, prereg = _fixture(tmp_path)
    rows[0]["turns"][0]["text"] = "请让 ReAct 选择 optionId __REPLACE__ 123456"
    rows[1]["generalizationClass"] = "adaptive_needed"
    _write_jsonl(scenarios, rows)
    prereg["datasetSha256"] = "0" * 64
    prereg["classCounts"] = {"adaptive_needed": 4, "deterministic_control": 2, "negative_control": 3}
    _write_json(tmp_path / "private" / "preregistration.json", prereg)

    report = validate_submission(package_root=tmp_path)

    codes = {failure["code"] for failure in report["failures"]}
    assert report["status"] == "HOLD"
    assert {
        "implementation_or_mapping_leak",
        "placeholder_present",
        "product_id_present",
        "class_balance_invalid",
        "dataset_hash_mismatch",
    } <= codes


def test_rejects_near_duplicate_against_forbidden_dataset(tmp_path: Path) -> None:
    scenarios, rows, prereg = _fixture(tmp_path / "submission")
    forbidden = tmp_path / "old.jsonl"
    _write_jsonl(forbidden, [{
        "scenarioId": "OLD-001",
        "turns": [
            rows[0]["turns"][0],
            {"turnId": "T2", "text": "这是完全不同且只存在于旧集合中的后续表达"},
        ],
    }])

    report = validate_submission(
        package_root=tmp_path / "submission",
        forbidden_dataset=forbidden,
    )

    codes = {failure["code"] for failure in report["failures"]}
    assert report["status"] == "HOLD"
    assert "forbidden_turn_near_duplicate" in codes
