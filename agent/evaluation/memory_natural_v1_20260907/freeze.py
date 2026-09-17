"""Secret-free immutable implementation snapshot before held-out flow runs."""
import ast
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[3]
OUT = Path("D:/agent-experiments/memory-natural-v1-20260907")


def main():
    destination = OUT / (sys.argv[1] if len(sys.argv) > 1 else "frozen001")
    destination.mkdir(exist_ok=False)
    worker = ROOT / "agent/app/memory_candidate_worker.py"
    node = next(n for n in ast.parse(worker.read_text(encoding="utf-8")).body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_extract_observed")
    ast_hash = hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()
    expected_ast = "e87f0a64bbe74b779ffa3bf5a31d916c85059e3c49da137bdd62851666f087af"
    evaluated = json.loads((OUT / "candidate-validation001/started.json").read_text(encoding="utf-8"))
    prior_hash = evaluated["sourceSha256"][str(worker)]
    if (ast_hash != expected_ast or
            prior_hash != "3f1c053a3cd841d296f4ed1668f049ab619bad6682138c86a2b05f0f0b555d68"):
        raise RuntimeError("extraction_evidence_reuse_rejected")
    paths = set((ROOT / "agent/app").rglob("*.py"))
    paths.update((ROOT / "agent/tests").glob("test_memory*.py"))
    paths.update(Path(__file__).parent.glob("*.py"))
    paths.add(ROOT / "agent/app/static/index.html")
    for relative in ("artifacts.py", "subscription.py", "private_redis.py", "catalog_transport.py"):
        paths.add(ROOT / "agent/evaluation/context_history_strategies_v1_20260905" / relative)
    hashes = {}
    for path in sorted(paths):
        relative = path.relative_to(ROOT)
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise RuntimeError("snapshot_drift")
        hashes[str(relative)] = expected
    receipt = {"sources": hashes, "modelCalls": 0,
        "extractionScopeReuse": {"evaluatedWorkerSha256": prior_hash,
            "beforeAndAfterExtractObservedAstSha256": ast_hash,
            "basis": "pre-edit live file hash matched evaluated receipt; AST captured before and after UI-only worker edit",
            "claim": "candidate extraction result reused only; new flow pipeline measured separately"}}
    with (destination / "manifest.json").open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"files": len(hashes), "extractionAstUnchanged": True}))


if __name__ == "__main__":
    main()
