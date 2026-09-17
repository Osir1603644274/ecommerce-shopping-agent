"""Create a new immutable, secret-free starting snapshot for the memory goal."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
OUTPUT = Path("D:/agent-experiments/memory-natural-v1-20260907")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    if OUTPUT.exists():
        raise SystemExit("output_exists_preserve_previous_snapshot")
    if shutil.disk_usage(OUTPUT.anchor).free < 10 * 1024**3:
        raise SystemExit("insufficient_output_space")
    sources = set((ROOT / "agent/app").rglob("*.py"))
    sources.update((ROOT / "agent/app/static").rglob("*.html"))
    sources.update((ROOT / "agent/tests").glob("*memory*.py"))
    sources.update((ROOT / "backend/src").rglob("*Memory*.java"))
    sources.update((ROOT / "agent/evaluation/memory_natural_v1_20260907").glob("*"))
    sources.update((ROOT / "agent/evaluation/assets/used_phone_memory_catalog_v13_20260830").glob("*.json*"))
    sources.update(ROOT / relative for relative in (
        "agent/pyproject.toml", "agent/tests/fake_redis.py",
        "docs/acceptance/shopping-memory-v18-architecture-and-evaluation-2026-08-30.md",
        "agent/evaluation/shopping_memory_v18_real_full_chain_20260830/attempt006/report.json",
        "agent/evaluation/shopping_memory_extraction_v17_20260830/attempt001/report.json",
    ))
    before = {str(p.relative_to(ROOT)).replace("\\", "/"): digest(p)
              for p in sorted(sources) if p.is_file() and not p.is_symlink()}
    for relative, expected in before.items():
        source = ROOT / relative
        target = OUTPUT / "baseline" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if digest(source) != expected or digest(target) != expected:
            raise RuntimeError("source_changed_during_snapshot:" + relative)
    manifest = {
        "schemaVersion": "memory-natural-baseline-v1",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "sourceRoot": str(ROOT), "outputRoot": str(OUTPUT),
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "python": sys.executable, "sources": before,
        "copiedSecrets": False, "modelCalls": 0,
        "preservedDirtyCheckout": True,
    }
    with (OUTPUT / "BASELINE.json").open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
    state = {
        "schemaVersion": "memory-natural-goal-state-v1", "phase": "P0",
        "status": "IMPLEMENTING", "modelCalls": 0, "productionEnabled": False,
        "completed": ["baseline_snapshot"],
        "remaining": ["durable_binding", "natural_candidates", "candidate_governance",
                      "runtime_integration", "regression", "pilot", "validation", "report"],
    }
    with (OUTPUT / "STATE.json").open("x", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"output": str(OUTPUT), "files": len(before), "modelCalls": 0}))


if __name__ == "__main__":
    main()
