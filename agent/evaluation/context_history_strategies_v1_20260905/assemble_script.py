"""Assemble an explicitly authored extension; never fabricate Agent replies."""
import argparse
import json
from pathlib import Path

from .artifacts import HERE, file_sha, write_new
from .dataset import validate


def run(extension_path, output):
    extension = json.loads(extension_path.read_text(encoding="utf-8"))
    parent = (HERE / extension["parentScript"]).resolve()
    if not parent.is_relative_to(HERE.resolve()) or file_sha(parent) != extension["parentSha256"]:
        raise ValueError("extension_parent_binding_mismatch")
    original = json.loads(parent.read_text(encoding="utf-8"))
    value = {**original, "scenarioId": "core-development-72-extension-v1",
             "turns": [*original["turns"], *extension["turns"]]}
    validate({"seedQuery": value["seedQuery"], "turns": value["turns"]}, seed=value["seedQuery"], turns=72)
    if original["split"] != "development" or extension["split"] != "development":
        raise ValueError("extension_requires_development_source")
    output.mkdir(parents=True, exist_ok=False)
    write_new(output / "script.json", value)
    write_new(output / "audit.json", {"status": "DEVELOPMENT_EXTENSION_ASSEMBLED",
        "parentSha256": file_sha(parent), "extensionSha256": file_sha(extension_path),
        "scriptSha256": file_sha(output / "script.json"), "author": extension["author"],
        "syntheticUserScriptOnly": True, "independentOf48TurnFamily": False,
        "actualTokenLength": "REQUIRES_SUT_REPLAY", "formalAcceptance": False})
    print(json.dumps({"status": "DEVELOPMENT_EXTENSION_ASSEMBLED", "turns": 72}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("extension", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    run(args.extension, args.output)
