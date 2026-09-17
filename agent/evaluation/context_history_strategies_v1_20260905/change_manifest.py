"""Audit current source against this study's preserved dirty baseline, not HEAD."""
import argparse
import json
from pathlib import Path

from .artifacts import HERE, ROOT, file_sha, now, write_new


def run(output: Path):
    baseline = HERE / "baseline/manifest.json"
    old = json.loads(baseline.read_text(encoding="utf-8"))["sources"]
    changed = [{"path": path, "baselineSha256": digest, "currentSha256": file_sha(ROOT / path)}
        for path, digest in old.items() if file_sha(ROOT / path) != digest]
    new_app = [{"path": path.relative_to(ROOT).as_posix(), "sha256": file_sha(path)}
        for path in sorted((ROOT / "agent/app").rglob("*.py")) if path.relative_to(ROOT).as_posix() not in old]
    experiment = [{"path": path.relative_to(ROOT).as_posix(), "sha256": file_sha(path)} for path in sorted(HERE.glob("*.py"))]
    write_new(output, {"at": now(), "baselineManifestSha256": file_sha(baseline), "changedBaselineFiles": changed,
        "newApplicationFiles": new_app, "experimentSources": experiment,
        "note": "Compared with the preserved pre-experiment dirty source, NOT git HEAD. This records byte differences, not ownership of all current git diffs. No commit, reset, checkout, clean or old attempt mutation."})
    print(json.dumps({"changedBaselineFiles": len(changed), "newApplicationFiles": len(new_app), "experimentSources": len(experiment)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    run(parser.parse_args().output)
