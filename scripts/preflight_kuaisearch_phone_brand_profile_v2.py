#!/usr/bin/env python3
"""Fail-closed preflight for a future KuaiSearch phone-profile v2 run.

This file intentionally does not execute an effect experiment.  A future v2
effect runner may call ``preflight`` before reading any evaluation identities.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


class PreflightError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_identity_hash(user_ids: list[int]) -> str:
    payload = json.dumps(sorted(user_ids), separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def contains_public_identities(value: Any, key: str = "") -> bool:
    forbidden = {"userids", "sealeduserids", "identities", "useridlist"}
    if key.casefold() in forbidden:
        return True
    if isinstance(value, dict):
        return any(contains_public_identities(child, str(child_key)) for child_key, child in value.items())
    if isinstance(value, list):
        return any(contains_public_identities(child) for child in value)
    return False


def preflight(
    manifest_path: Path,
    external_freeze_receipt_path: Path,
    private_sealed_map_path: Path,
    public_root: Path,
) -> dict[str, Any]:
    manifest_path = manifest_path.resolve()
    external_freeze_receipt_path = external_freeze_receipt_path.resolve()
    private_sealed_map_path = private_sealed_map_path.resolve()
    public_root = public_root.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schemaVersion") != "kuaisearch-lite-phone-brand-profile-preregistration-v2":
        raise PreflightError("MANIFEST_SCHEMA_MISMATCH")
    if contains_public_identities(manifest):
        raise PreflightError("PUBLIC_MANIFEST_CONTAINS_SEALED_IDENTITIES")

    own_expected = manifest.get("code", {}).get("preflightRunnerSha256")
    own_actual = sha256_file(Path(__file__).resolve())
    if own_expected != own_actual:
        raise PreflightError("PREFLIGHT_RUNNER_HASH_MISMATCH")

    dependencies = manifest.get("code", {}).get("dependencies")
    if not isinstance(dependencies, list) or not dependencies:
        raise PreflightError("DEPENDENCY_BINDINGS_MISSING")
    dependency_receipts = []
    for dependency in dependencies:
        path = Path(dependency["path"]).resolve()
        if not path.is_file():
            raise PreflightError(f"DEPENDENCY_MISSING:{path}")
        actual = sha256_file(path)
        if actual != dependency.get("sha256"):
            raise PreflightError(f"DEPENDENCY_HASH_MISMATCH:{path}")
        dependency_receipts.append({"path": str(path), "sha256": actual})

    if is_within(external_freeze_receipt_path, public_root):
        raise PreflightError("FREEZE_RECEIPT_NOT_EXTERNAL_TO_PUBLIC_WORKSPACE")
    freeze = json.loads(external_freeze_receipt_path.read_text(encoding="utf-8"))
    manifest_hash = sha256_file(manifest_path)
    if freeze.get("manifestSha256") != manifest_hash:
        raise PreflightError("EXTERNAL_FREEZE_MANIFEST_HASH_MISMATCH")
    if freeze.get("appendOnly") is not True or not freeze.get("authority") or not freeze.get("recordedAt"):
        raise PreflightError("EXTERNAL_FREEZE_AUTHORITY_INCOMPLETE")

    if is_within(private_sealed_map_path, public_root):
        raise PreflightError("SEALED_MAP_IS_PUBLIC")
    sealed = manifest.get("sealedAssignment", {})
    actual_private_hash = sha256_file(private_sealed_map_path)
    if actual_private_hash != sealed.get("privateMappingSha256"):
        raise PreflightError("PRIVATE_SEALED_MAP_HASH_MISMATCH")
    private_map = json.loads(private_sealed_map_path.read_text(encoding="utf-8"))
    user_ids = private_map.get("userIds")
    if not isinstance(user_ids, list) or not user_ids or len(user_ids) != len(set(user_ids)):
        raise PreflightError("PRIVATE_SEALED_IDENTITIES_INVALID")
    normalized_ids = [int(value) for value in user_ids]
    identity_hash = stable_identity_hash(normalized_ids)
    if identity_hash != sealed.get("publicIdentitySetSha256"):
        raise PreflightError("SEALED_IDENTITY_SET_HASH_MISMATCH")
    if len(normalized_ids) != sealed.get("identityCount"):
        raise PreflightError("SEALED_IDENTITY_COUNT_MISMATCH")

    return {
        "status": "PREFLIGHT_ACCEPT",
        "manifestSha256": manifest_hash,
        "preflightRunnerSha256": own_actual,
        "dependencyReceipts": dependency_receipts,
        "externalFreezeReceiptSha256": sha256_file(external_freeze_receipt_path),
        "privateSealedMapSha256": actual_private_hash,
        "sealedIdentitySetSha256": identity_hash,
        "sealedIdentityCount": len(normalized_ids),
        "publicIdentitiesExposed": False,
        "effectExecuted": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--external-freeze-receipt", type=Path, required=True)
    parser.add_argument("--private-sealed-map", type=Path, required=True)
    parser.add_argument("--public-root", type=Path, required=True)
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    if not args.preflight_only:
        raise PreflightError("EFFECT_EXECUTION_NOT_IMPLEMENTED_IN_V2_PREFLIGHT_RUNNER")
    result = preflight(
        args.manifest,
        args.external_freeze_receipt,
        args.private_sealed_map,
        args.public_root,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
