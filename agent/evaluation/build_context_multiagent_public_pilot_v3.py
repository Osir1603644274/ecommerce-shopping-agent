"""Freeze the MA2 communication-remediation public development pilot."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "evaluation/context-multiagent-public-pilot-v2.json"
TARGET = ROOT / "evaluation/context-multiagent-public-pilot-v3.json"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    source["schemaVersion"] = "context-multiagent-public-pilot-manifest-v3"
    source["status"] = "FROZEN_BEFORE_EXECUTION"
    source["attemptId"] = "context-multiagent-public-pilot-v3-attempt001"
    source["description"] = "Open-development MA2 remediation: bound request/reply envelope plus selection-critical verified decision support."
    source["sourceFiles"] = {
        "agent/evaluation/context_multiagent_public_pilot_v3.py": sha(ROOT / "agent/evaluation/context_multiagent_public_pilot_v3.py"),
        "agent/app/multi_agent_v2.py": sha(ROOT / "agent/app/multi_agent_v2.py"),
        "agent/app/evidence_research_v1.py": sha(ROOT / "agent/app/evidence_research_v1.py"),
        "agent/app/context_compiler_v1.py": sha(ROOT / "agent/app/context_compiler_v1.py"),
    }
    source["scope"] = "PUBLIC_DEVELOPMENT_REMEDIATION_NOT_UNTOUCHED_CONFIRMATION"
    source["productionDefaultsChanged"] = False
    TARGET.write_text(json.dumps(source, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"status": "FROZEN", "manifest": str(TARGET), "sources": len(source["sourceFiles"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
