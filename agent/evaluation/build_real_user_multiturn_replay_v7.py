"""Build V7 with receipt schema constants derived from the final authority."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "agent/evaluation/real_user_multiturn_replay_20260903_v7"
PACKAGE_ID = "real_user_multiturn_replay_20260903_v7"


def _load_v5_builder():
    path = ROOT / "agent/evaluation/build_real_user_multiturn_replay_v5.py"
    spec = importlib.util.spec_from_file_location("real_user_multiturn_v5_builder_for_v7", path)
    if not spec or not spec.loader:
        raise RuntimeError("cannot load V5 package builder")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    builder = _load_v5_builder()
    builder.TARGET = TARGET
    builder.PACKAGE_ID = PACKAGE_ID
    builder.main()

    verify_path = TARGET / "verify_package.py"
    verify_path.write_text(
        verify_path.read_text(encoding="utf-8").replace(
            'PID="real_user_multiturn_replay_20260903_v5"',
            f'PID="{PACKAGE_ID}"',
        ),
        encoding="utf-8",
        newline="\n",
    )
    (TARGET / "README.md").write_text(
        """# Real-user multi-turn Context A/B V7

Status before execution: `PREREGISTERED_READY / NO_FORMAL_ATTEMPT`.

V7 preserves the byte-identical real-user dataset and corrected per-turn durable identity. It derives every receipt schema constant from the final execution authority, fixing the V6 preformal stale-constant failure. Multi-Agent remains disabled equally in both arms to isolate Context.
""",
        encoding="utf-8",
        newline="\n",
    )
    prereg_path = TARGET / "preregistration.md"
    prereg_path.write_text(
        prereg_path.read_text(encoding="utf-8").replace("V5 preregistration", "V7 preregistration"),
        encoding="utf-8",
        newline="\n",
    )

    authority_path = TARGET / "execution_authority.json"
    authority = json.loads(authority_path.read_text(encoding="utf-8"))
    authority["repairFromV6"] = {
        "v6Status": "PREFORMAL_HOLD",
        "v6Failure": "receipt schema retained stale V4 authority/configuration constants",
        "v6FormalAttemptExecuted": False,
        "detectedBy": "42-row fixture execution before formal attempt",
    }
    authority.pop("repairFromV4", None)
    builder.write_json(authority_path, authority)

    receipt_schema_path = TARGET / "execution_receipt.schema.json"
    receipt_schema = json.loads(receipt_schema_path.read_text(encoding="utf-8"))
    properties = receipt_schema["properties"]
    properties["packageId"] = {"const": PACKAGE_ID}
    properties["executionAuthoritySha256"] = {"const": builder.sha_file(authority_path)}
    properties["executionConfigSnapshotSha256"] = {
        "const": builder.sha_file(TARGET / "execution_config_snapshot.json")
    }
    trace_properties = properties["expectedTraceHashes"]["properties"]
    for field, digest in authority["expectedTraceHashes"].items():
        trace_properties[field] = {"const": digest}
    builder.write_json(receipt_schema_path, receipt_schema)

    inventory = sorted(
        path for path in TARGET.iterdir()
        if path.is_file() and path.name not in {"SHA256SUMS.txt", "verification.json", "package_manifest.json"}
    )
    manifest = {
        "schemaVersion": "real-user-multiturn-package-manifest-v7",
        "packageId": PACKAGE_ID,
        "status": "PREREGISTERED_READY_NO_FORMAL_ATTEMPT",
        "parentDatasetPackage": builder.OLD_ID,
        "conversationCount": 8,
        "turnCount": 21,
        "pairedOutputCount": 42,
        "files": {path.name: builder.sha_file(path) for path in inventory},
    }
    builder.write_json(TARGET / "package_manifest.json", manifest)
    inventory = sorted(
        path for path in TARGET.iterdir()
        if path.is_file() and path.name not in {"SHA256SUMS.txt", "verification.json"}
    )
    (TARGET / "SHA256SUMS.txt").write_text(
        "".join(f"{builder.sha_file(path)}  {path.name}\n" for path in inventory),
        encoding="utf-8",
        newline="\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
