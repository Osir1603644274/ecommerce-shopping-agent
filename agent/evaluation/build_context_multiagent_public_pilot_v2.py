"""Build the versioned V2 manifest after the compatibility repair is frozen."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "evaluation/context-multiagent-public-pilot-v1.json"
TARGET = ROOT / "evaluation/context-multiagent-public-pilot-v2.json"


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_manifest() -> dict:
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    result = copy.deepcopy(source)
    result["schemaVersion"] = "context-multiagent-public-pilot-manifest-v2"
    result["createdAt"] = "2026-08-31T00:00:00Z"
    result["repairFrom"] = {
        "attemptId": "context-multiagent-public-pilot-v1-attempt001",
        "path": "agent/evaluation/runs/context_multiagent_public_pilot_v1_attempt001",
        "failureClass": "SHARED_PROVIDER_COMPATIBILITY",
        "qualityInferenceAllowed": False,
    }
    result["model"]["toolRequestThinkingMode"] = "disabled"
    result["providerCompatibilityAdapter"] = {
        "scope": "tool-bearing requests only",
        "forcedToolChoicePreserved": True,
        "compatibilitySmokeRequired": True,
        "scoredSmoke": False,
    }
    result["sourceFiles"].update(
        {
            "agent/evaluation/context_multiagent_public_pilot_v2.py": file_hash(
                ROOT / "agent/evaluation/context_multiagent_public_pilot_v2.py"
            ),
            "agent/evaluation/build_context_multiagent_public_pilot_v2.py": file_hash(
                ROOT / "agent/evaluation/build_context_multiagent_public_pilot_v2.py"
            ),
            "agent/tests/test_context_multiagent_public_pilot_v2.py": file_hash(
                ROOT / "agent/tests/test_context_multiagent_public_pilot_v2.py"
            ),
            "docs/adr/context-multiagent-v1-implementation-amendment-003.md": file_hash(
                ROOT / "docs/adr/context-multiagent-v1-implementation-amendment-003.md"
            ),
        }
    )
    result["stopRules"]["compatibilitySmokeBeforeAttempt002"] = True
    result["stopRules"]["attempt001Preserved"] = True
    return result


def main() -> None:
    TARGET.write_text(
        json.dumps(build_manifest(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


if __name__ == "__main__":
    main()
