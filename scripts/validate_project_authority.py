from __future__ import annotations

import json
import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "docs" / "governance" / "project-authority.json"
EXPECTED = ROOT / "docs" / "governance" / "expected-authority-artifacts.json"


def main() -> int:
    data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    if data.get("schemaVersion") != "project-authority-v1":
        raise SystemExit("invalid schemaVersion")
    if expected.get("schemaVersion") != "project-authority-expected-artifacts-v1":
        raise SystemExit("invalid expected-artifacts schemaVersion")

    lifecycle_values = set(data.get("lifecycleValues", []))
    decision_values = set(data.get("decisionValues", []))
    entries = data.get("entries")
    if not isinstance(entries, list) or not entries:
        raise SystemExit("entries must be a non-empty list")

    seen_ids: set[str] = set()
    seen_topics: set[str] = set()
    errors: list[str] = []
    for entry in entries:
        entry_id = entry.get("id")
        topic = entry.get("topic")
        relative_path = entry.get("path")
        if not isinstance(entry_id, str) or not entry_id:
            errors.append("entry has invalid id")
            continue
        if entry_id in seen_ids:
            errors.append(f"duplicate id: {entry_id}")
        seen_ids.add(entry_id)

        if not isinstance(topic, str) or not topic:
            errors.append(f"{entry_id}: invalid topic")
        elif topic in seen_topics:
            errors.append(f"duplicate topic: {topic}")
        else:
            seen_topics.add(topic)

        if entry.get("lifecycle") not in lifecycle_values:
            errors.append(f"{entry_id}: invalid lifecycle")
        if entry.get("decision") not in decision_values:
            errors.append(f"{entry_id}: invalid decision")
        if not isinstance(entry.get("claimBoundary"), str) or not entry["claimBoundary"]:
            errors.append(f"{entry_id}: missing claimBoundary")
        if not isinstance(relative_path, str) or not relative_path:
            errors.append(f"{entry_id}: invalid path")
        else:
            target = ROOT / relative_path
            if not target.exists():
                errors.append(f"{entry_id}: missing path: {relative_path}")
            expected_sha256 = entry.get("sha256")
            if expected_sha256 is not None:
                if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
                    errors.append(f"{entry_id}: invalid sha256")
                elif not target.is_file():
                    errors.append(f"{entry_id}: sha256 target must be a file")
                else:
                    actual_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
                    if actual_sha256 != expected_sha256.lower():
                        errors.append(
                            f"{entry_id}: sha256 mismatch: {actual_sha256} != {expected_sha256}"
                        )

        if entry.get("lifecycle") == "SEALED" and entry.get("decision") == "IN_PROGRESS":
            errors.append(f"{entry_id}: sealed entry cannot be in progress")

    entries_by_id = {
        entry["id"]: entry
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    }
    expected_entries = expected.get("expectedAuthorityEntries")
    if not isinstance(expected_entries, list) or not expected_entries:
        errors.append("expectedAuthorityEntries must be a non-empty list")
        expected_entries = []
    expected_ids: set[str] = set()
    for expected_entry in expected_entries:
        expected_id = expected_entry.get("id") if isinstance(expected_entry, dict) else None
        if not isinstance(expected_id, str) or not expected_id:
            errors.append("expected authority entry has invalid id")
            continue
        if expected_id in expected_ids:
            errors.append(f"duplicate expected authority id: {expected_id}")
            continue
        expected_ids.add(expected_id)
        actual = entries_by_id.get(expected_id)
        if actual is None:
            errors.append(f"missing expected authority entry: {expected_id}")
            continue
        for field in ("topic", "path", "lifecycle", "decision"):
            if actual.get(field) != expected_entry.get(field):
                errors.append(
                    f"{expected_id}: expected {field}={expected_entry.get(field)!r}, "
                    f"found {actual.get(field)!r}"
                )
        if actual.get("authoritative") is not True:
            errors.append(f"{expected_id}: expected authoritative=true")
        if expected_entry.get("requireSha256") is True and not actual.get("sha256"):
            errors.append(f"{expected_id}: expected sha256 is missing")

    authoritative_ids = {
        entry_id
        for entry_id, entry in entries_by_id.items()
        if entry.get("authoritative") is True
    }
    unlisted_authoritative = authoritative_ids - expected_ids
    if unlisted_authoritative:
        errors.append(
            "authoritative entries missing from completeness contract: "
            + ", ".join(sorted(unlisted_authoritative))
        )

    blockers = data.get("blockers")
    if not isinstance(blockers, list):
        errors.append("blockers must be a list")
        blockers = []
    blocker_ids: set[str] = set()
    for blocker in blockers:
        blocker_id = blocker.get("id") if isinstance(blocker, dict) else None
        if not isinstance(blocker_id, str) or not blocker_id:
            errors.append("blocker has invalid id")
            continue
        if blocker_id in blocker_ids:
            errors.append(f"duplicate blocker id: {blocker_id}")
        blocker_ids.add(blocker_id)
        evidence_path = blocker.get("evidencePath")
        if not isinstance(evidence_path, str) or not evidence_path:
            errors.append(f"{blocker_id}: missing evidencePath")
        elif not (ROOT / evidence_path).exists():
            errors.append(f"{blocker_id}: missing evidence path: {evidence_path}")
        if blocker.get("status") not in {"OPEN", "RESOLVED"}:
            errors.append(f"{blocker_id}: invalid blocker status")

    expected_known_blockers = expected.get("expectedKnownBlockers")
    if not isinstance(expected_known_blockers, list):
        errors.append("expectedKnownBlockers must be a list")
        expected_known_blocker_ids: set[str] = set()
    else:
        expected_known_blocker_ids = set(expected_known_blockers)
        if len(expected_known_blocker_ids) != len(expected_known_blockers):
            errors.append("duplicate expected known blocker id")
    if blocker_ids != expected_known_blocker_ids:
        errors.append(
            "known blocker set mismatch: expected "
            + ", ".join(sorted(expected_known_blocker_ids))
            + "; found "
            + ", ".join(sorted(blocker_ids))
        )

    expected_blockers = expected.get("expectedOpenBlockers")
    if not isinstance(expected_blockers, list):
        errors.append("expectedOpenBlockers must be a list")
        expected_blocker_ids: set[str] = set()
    else:
        expected_blocker_ids = set(expected_blockers)
        if len(expected_blocker_ids) != len(expected_blockers):
            errors.append("duplicate expected blocker id")
    open_blocker_ids = {
        blocker["id"]
        for blocker in blockers
        if isinstance(blocker, dict) and blocker.get("status") == "OPEN"
    }
    if open_blocker_ids != expected_blocker_ids:
        errors.append(
            "open blocker set mismatch: expected "
            + ", ".join(sorted(expected_blocker_ids))
            + "; found "
            + ", ".join(sorted(open_blocker_ids))
        )

    if not any(entry.get("authoritative") for entry in entries):
        errors.append("registry has no authoritative entry")
    if errors:
        raise SystemExit("\n".join(errors))

    print(
        json.dumps(
            {
                "status": "PASS",
                "schemaVersion": data["schemaVersion"],
                "entryCount": len(entries),
                "expectedAuthorityEntryCount": len(expected_entries),
                "knownBlockerCount": len(blockers),
                "openBlockerCount": len(open_blocker_ids),
                "missingPathCount": 0,
                "duplicateIdCount": 0,
                "duplicateTopicCount": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
