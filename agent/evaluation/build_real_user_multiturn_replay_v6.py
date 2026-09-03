"""Build V6 after the V5 preformal receipt-producer contract failure."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "agent/evaluation/real_user_multiturn_replay_20260903_v6"
PACKAGE_ID = "real_user_multiturn_replay_20260903_v6"


def _load_v5_builder():
    path = ROOT / "agent/evaluation/build_real_user_multiturn_replay_v5.py"
    spec = importlib.util.spec_from_file_location("real_user_multiturn_v5_builder", path)
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
    readme = """# Real-user multi-turn Context A/B V6

Status before execution: `PREREGISTERED_READY / NO_FORMAL_ATTEMPT`.

V6 preserves the byte-identical real-user dataset and V5 identity policy. It repairs the V5 preformal receipt contract mismatch by making the runner producer equal the schema constant. Multi-Agent remains disabled equally in both arms to isolate Context.
"""
    (TARGET / "README.md").write_text(readme, encoding="utf-8", newline="\n")
    prereg = (TARGET / "preregistration.md").read_text(encoding="utf-8")
    prereg = prereg.replace("V5 preregistration", "V6 preregistration")
    (TARGET / "preregistration.md").write_text(prereg, encoding="utf-8", newline="\n")

    authority_path = TARGET / "execution_authority.json"
    authority = json.loads(authority_path.read_text(encoding="utf-8"))
    authority["repairFromV5"] = {
        "v5Status": "PREFORMAL_HOLD",
        "v5Failure": "runner receipt producer did not equal the preregistered schema constant",
        "v5FormalAttemptExecuted": False,
        "detectedBy": "42-row fixture execution before formal attempt",
    }
    authority.pop("repairFromV4", None)
    builder.write_json(authority_path, authority)

    inventory = sorted(
        path for path in TARGET.iterdir()
        if path.is_file() and path.name not in {"SHA256SUMS.txt", "verification.json", "package_manifest.json"}
    )
    manifest = {
        "schemaVersion": "real-user-multiturn-package-manifest-v6",
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
