import hashlib
import json
from pathlib import Path

from evaluation.used_phone_react_generalization_human_gate_v1 import score_human_gate


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")


def test_human_gate_unblinds_multiple_valid_reviews(tmp_path: Path) -> None:
    source = tmp_path / "public.jsonl"
    dataset = tmp_path / "dataset.jsonl"
    packet = tmp_path / "packet.md"
    prereg = tmp_path / "prereg.json"
    mapping = tmp_path / "mapping.json"
    output = tmp_path / "score.json"
    source.write_text(
        json.dumps({"itemId": "item-1", "scenarioId": "scenario-1"}) + "\n",
        encoding="utf-8",
    )
    dataset.write_text(
        json.dumps({"scenarioId": "scenario-1", "generalizationClass": "adaptive_needed"}) + "\n",
        encoding="utf-8",
    )
    packet.write_text("blind packet\n", encoding="utf-8")
    packet_sha = hashlib.sha256(packet.read_bytes()).hexdigest()
    _write_json(mapping, {"items": [{"itemId": "item-1", "labels": {"A": "react_v0", "B": "fixed_v1"}}]})
    _write_json(prereg, {
        "humanDirectionGate": {
            "minimumDirectFullPacketReviewers": 2,
            "minimumReactPreferenceShareAmongJudgeable": 0.6,
            "requireNoLowerConstraintFidelityMeanByClass": True,
            "requireNoLowerEvidenceDisciplineMeanByClass": True,
        },
        "claimBoundary": {"canAcceptLimitedHeldoutDirection": True},
    })
    ratings_paths = []
    for index in (1, 2):
        path = tmp_path / f"review-{index}.json"
        _write_json(path, {
            "reviewerId": f"reviewer-{index}",
            "packetSha256": packet_sha,
            "protocolStatus": "VALID_BLIND_REVIEW",
            "items": [{
                "itemId": "item-1",
                "candidateA": [5, 5, 5, 5],
                "candidateB": [4, 4, 4, 4],
                "overallPreference": "A",
            }],
        })
        ratings_paths.append(path)

    result = score_human_gate(
        source_public=source,
        packet=packet,
        dataset=dataset,
        preregistration=prereg,
        sealed_mapping=mapping,
        ratings_paths=ratings_paths,
        output=output,
    )

    assert result["status"] == "ACCEPT_LIMITED_DIRECTION"
    assert result["preferenceCounts"]["react_v0"] == 2
    assert result["reactPreferenceShareAmongJudgeable"] == 1.0
    assert result["dimensionMeans"]["react_v0"]["constraintFidelity"] == 5.0
