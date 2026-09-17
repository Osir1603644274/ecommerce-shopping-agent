"""Validate declared development-only seed provenance without accessing qrels."""
import argparse
import json
from pathlib import Path

from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, file_sha, write_new
from agent.evaluation.context_history_strategies_v1_20260905.dataset import validate


def audit(path, output):
    value = json.loads(path.read_text(encoding="utf-8"))
    split_path = HERE / "seed_split001.json"
    split = json.loads(split_path.read_text(encoding="utf-8"))
    family = next(item for item in split["families"] if item["familyId"] == value["familyId"])
    if family["split"] != "development" or value["split"] != "development":
        raise ValueError("not_a_development_seed")
    seed = next(item for item in family["members"] if item["query_id"] == value["seedProvenance"]["originalQueryId"])
    validate({"seedQuery": value["seedQuery"], "turns": value["turns"]}, seed=seed["query"], turns=len(value["turns"]))
    result = {"status": "DEV_SCRIPT_STRUCTURE_AND_SEED_PASS_NOT_SUT_QUALITY", "scriptSha256": file_sha(path),
        "splitSha256": file_sha(split_path), "familyId": value["familyId"], "turns": len(value["turns"]),
        "userCharacters": sum(len(row["userText"]) for row in value["turns"]),
        "longNoteTurns": [row["turn"] for row in value["turns"] if row["intent"] == "note" and len(row["userText"]) >= 250],
        "qrelsRead": False, "prewrittenAgentAnswers": False, "realDialogue": False, "formalAcceptance": False}
    write_new(output, result)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("script", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    audit(args.script, args.output)
