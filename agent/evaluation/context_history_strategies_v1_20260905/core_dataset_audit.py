"""Pre-SUT semantic correction of a generated development script, preserving v1."""
import argparse
import json
from pathlib import Path

from .artifacts import HERE, file_sha, write_new
from .dataset import validate


def run(output):
    source = HERE / "core_dataset001/script.json"
    if file_sha(source) != "931485fcebb611e7a333bbe6ef3a1bb9a6c8766977e1f904f891c4836fb33a7b":
        raise ValueError("unaudited_source_revision")
    value = json.loads(source.read_text(encoding="utf-8"))
    changes = {
        13: ("第11轮改变了硬条件，请先按新条件重新检索，再仅比较这次实际展示的第一款和第二款；若不足两款就如实说明，不补造对象。第10轮旧批次只留作历史，不能冒充当前可执行清单。",
             "Original comparison requested active action on a superseded batch. Core flow now explicitly requests a fresh eligible scope; historical-only recalls elsewhere remain intact."),
        48: ("请最后整理当前硬条件、软偏好、购买对象和用途，并汇总目前有效的验机安排、配件与运输约定、迁移日程、核实清单和检查次序；把已经撤销或替换的旧值单列，注意后来恢复的条件。然后只比较第47轮后最近实际展示的第一款和第二款；不足两款就如实说明，不补造商品，也不要下单或支付。",
             "Original final query repeated nearly every current condition and old value, leaking the requested recall into the question. Replacement asks for retrieval/synthesis without supplying the answer."),
    }
    audit = []
    for turn, (text, reason) in changes.items():
        row = value["turns"][turn - 1]
        audit.append({"turn": turn, "originalUserText": row["userText"], "revisedUserText": text, "reason": reason})
        row["userText"] = text
    value["scenarioId"] = "core-development-48-revision2"
    value["preSutRevisionOf"] = str(source)
    validate({key: value[key] for key in ("seedQuery", "turns")}, seed=value["seedQuery"], turns=48)
    output.mkdir(parents=True, exist_ok=False)
    write_new(output / "script.json", value)
    write_new(output / "audit.json", {"status": "DEVELOPMENT_SCRIPT_SEMANTIC_AUDIT_PASS_FOR_REPLAY",
        "sourceSha256": file_sha(source), "scriptSha256": file_sha(output / "script.json"), "changes": audit,
        "author": "PRIMARY_AGENT_PRE_SUT_AUDIT", "sourceSutOutputsObserved": False,
        "scope": "Supported-field core development. Whole original script reviewed; explicit withdrawals, current/old value distinctions and 8 distinct substantive notes retained.",
        "formalAcceptance": False, "actualInputLengthStillRequiresReplay": True})
    print(json.dumps({"status": "DEV_SCRIPT_AUDITED", "changedTurns": list(changes)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    run(parser.parse_args().output)
