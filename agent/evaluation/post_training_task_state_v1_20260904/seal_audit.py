"""Seal supplemental audit artifacts after all checks and reporting have finished."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import xml.etree.ElementTree as ET

from .common import PACKAGE_DIR as ROOT, read_json, sha256_file, write_json, canonical_json


FILES = (
    "README.md", "experiment_plan.md", "SHA256SUMS.txt", "verification_final.json",
    "completion_audit_v2.py", "template_surface_audit.py", "final_binding_check.py", "seal_audit.py",
    "audit_v2/pre_eval_receipt.json", "audit_v2/verification.json",
    "audit_v2/final_binding_verification.json", "audit_v2/template_surface_isolation.json",
    "audit_v2/targeted_tests.xml", "audit_v2/DATA_CONTRACT_AND_SCOPE.md",
    "audit_v2/TRAINING_RESULT.md", "audit_v2/GOAL_REQUIREMENTS.md", "audit_v2/COMPLETION_AUDIT.md",
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    output = ROOT / "audit_v2/evidence_manifest.json"
    if args.verify:
        record = read_json(output)
        if set(record["files"]) != set(FILES):
            raise RuntimeError("audit manifest coverage changed")
        for name, item in record["files"].items():
            path = ROOT / name
            if path.stat().st_size != item["bytes"] or sha256_file(path) != item["sha256"]:
                raise RuntimeError("audit manifest mismatch: " + name)
        print(canonical_json({"status": "PASS", "verifiedFiles": len(FILES), "manifestSha256": sha256_file(output)}))
        return
    if output.exists():
        raise FileExistsError(output)
    for name in ("verification_final.json", "audit_v2/verification.json", "audit_v2/final_binding_verification.json", "audit_v2/template_surface_isolation.json"):
        if read_json(ROOT / name)["status"] != "PASS":
            raise RuntimeError("cannot seal failed audit: " + name)
    suites = ET.parse(ROOT / "audit_v2/targeted_tests.xml").getroot().findall("testsuite")
    total_tests = sum(int(s.attrib["tests"]) for s in suites)
    bad_tests = sum(int(s.attrib[k]) for s in suites for k in ("errors", "failures", "skipped"))
    if total_tests != 25 or bad_tests:
        raise RuntimeError("unexpected targeted test result")
    record = {"schemaVersion": "posttraining-audit-evidence-manifest-v1",
              "observedAtUtc": datetime.now(timezone.utc).isoformat(),
              "status": "PASS", "testCount": total_tests,
              "scope": "Local audit integrity seal, not a remote timestamp or training reproduction attestation.",
              "files": {name: {"bytes": (ROOT / name).stat().st_size, "sha256": sha256_file(ROOT / name)} for name in FILES}}
    write_json(output, record)
    print(canonical_json({"status": "PASS", "sealedFiles": len(FILES), "output": str(output), "manifestSha256": sha256_file(output)}))


if __name__ == "__main__":
    main()
