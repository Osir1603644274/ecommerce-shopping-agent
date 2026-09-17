"""Check real split-specific surface templates, not prefixed family identifiers."""
from __future__ import annotations

from itertools import combinations
from datetime import datetime, timezone

from . import build_dataset as source
from .common import PACKAGE_DIR, read_json, sha256_file, write_json, canonical_json


GROUPS = (
    "INITIAL_BUDGET_TEMPLATES", "INITIAL_BRAND_TEMPLATES",
    "EXISTING_OVERRIDE_TEMPLATES", "EXISTING_REMOVE_TEMPLATES",
    "UNCHANGED_TEMPLATES", "BLOCKING_TEMPLATES", "RESOLVE_TEMPLATES",
    "PHRASE_WRAPPERS",
)


def strings(value):
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return set().union(*(strings(v) for v in value.values()))
    return set().union(*(strings(v) for v in value))


def main():
    output = PACKAGE_DIR / "audit_v2/template_surface_isolation.json"
    if output.exists():
        raise FileExistsError(output)
    frozen = read_json(PACKAGE_DIR / "frozen_config.json")
    generator_hash = sha256_file(PACKAGE_DIR / "build_dataset.py")
    if generator_hash != frozen["scriptHashes"]["build_dataset.py"]:
        raise RuntimeError("generator differs from frozen source")
    checks, counts = [], {}
    for name in GROUPS:
        group = getattr(source, name)
        split_sets = {s: strings(group[s]) for s in ("train", "dev", "test")}
        counts[name] = {s: len(v) for s, v in split_sets.items()}
        for left, right in combinations(split_sets, 2):
            overlap = sorted(split_sets[left] & split_sets[right])
            checks.append({"group": name, "left": left, "right": right,
                           "passed": not overlap, "overlap": overlap})
    result = {
        "schemaVersion": "posttraining-surface-template-audit-v1",
        "observedAtUtc": datetime.now(timezone.utc).isoformat(),
        "sourceSha256": generator_hash,
        "status": "PASS" if all(c["passed"] for c in checks) else "FAIL",
        "checks": checks, "templateCounts": counts,
        "sharedSemanticSources": ["CASE_TYPES", "CONTROLLED_PHONE", "NEGATIVE_PHONE", "SPECS", "BUDGETS", "BRANDS"],
        "scope": "Literal split-specific surface-template strings are disjoint. Shared semantic phrases and value dictionaries remain; this does not prove novel-task generalization or IID samples.",
        "testResultsUsed": False,
        "changesPrimaryGate": False,
    }
    write_json(output, result)
    print(canonical_json({"status": result["status"], "checkCount": len(checks), "output": str(output)}))
    if result["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
