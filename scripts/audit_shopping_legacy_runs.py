"""Count resumable-looking legacy shopping checkpoints without exposing user data."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.app.api.commerce_demo import _redis
from agent.app.catalog_service import fingerprint, workflow_code_binding
from agent.app.catalog_fast_selection import selection_file
from agent.app.settings import settings


_BINDING_PATHS = ("catalog_service.py catalog_requirements.py catalog_worker.py "
    "catalog_conversation.py catalog_model_client.py api/catalog_workspace.py "
    "api/workspace_answer_stream.py catalog_fast_retrieval.py catalog_fast_retrieval_v2.py "
    "catalog_fast_retrieval_v3.py catalog_fast_selection.py product_followup.py "
    "api/commerce_controls.py catalog_execution_view.py catalog_commerce.py "
    "catalog_remote.py catalog_search_server.py catalog_react.py "
    "control/react_context.py control/react_decision.py").split()


def baseline_binding(manifest: Path) -> str:
    old = {row["path"].removeprefix("agent/app/"): row["sha256"]
           for row in json.loads(manifest.read_text(encoding="utf-8"))["affectedFiles"]}
    base = Path(__file__).resolve().parents[1] / "agent" / "app"
    config = selection_file(settings.catalog_workspace_fast_index_dir,
                            settings.catalog_workspace_fast_index_version)
    return fingerprint({"code": {name: old.get(name) or hashlib.sha256((base / name).read_bytes()).hexdigest()
                                 for name in _BINDING_PATHS},
        "fastEnabled": settings.catalog_workspace_fast_enabled,
        "reuseModelClient": settings.catalog_workspace_reuse_model_client,
        "externalCommerceEnabled": settings.commerce_workspace_external_catalog_enabled,
        "searchServiceUrl": settings.catalog_search_service_url,
        "fastConfiguration": hashlib.sha256(config.read_bytes()).hexdigest()
            if settings.catalog_workspace_fast_enabled else None,
        "modelPath": settings.catalog_workspace_model_path})


async def audit(baseline_manifest: Path | None = None) -> dict:
    client = _redis()
    current_binding = workflow_code_binding()
    old_binding = baseline_binding(baseline_manifest) if baseline_manifest else None
    statuses: Counter[str] = Counter()
    active: Counter[str] = Counter()
    matching = 0
    matching_old = 0
    async for key in client.scan_iter(match="commerce:workspace:*:run", count=100):
        raw = await client.get(key)
        if raw is None:
            continue
        run = json.loads(raw)
        status = run.get("status", "unknown")
        statuses[status] += 1
        if (run.get("workflow") == "catalog_workspace_v1"
                and not run.get("intent") and status not in {"completed", "ended"}):
            active[f"{status}:phase{run.get('catalogPhase', 'unknown')}"] += 1
            matching += int(run.get("catalogCodeBinding") == current_binding)
            matching_old += int(run.get("catalogCodeBinding") == old_binding)
    return {"capturedAtUtc": datetime.now(timezone.utc).isoformat(),
            "runStatuses": dict(statuses), "legacyOpenByStatusAndPhase": dict(active),
            "legacyOpen": sum(active.values()), "sameCodeBinding": matching,
            "sameBaselineBinding": matching_old, "baselineCodeBinding": old_binding,
            "currentCodeBinding": current_binding,
            "conclusion": "RETAIN_LEGACY_BRANCH" if active else "NO_LEGACY_OPEN_RUNS"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline-manifest", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    result = asyncio.run(audit(args.baseline_manifest))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: result[key] for key in
        ("legacyOpen", "sameCodeBinding", "sameBaselineBinding", "conclusion")}))


if __name__ == "__main__":
    main()
