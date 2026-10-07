"""Publish an audited public snapshot without touching the protected Git worktree.

Commands: init, plan, publish, verify. All mutable state lives below
``.runtime/public-release``; the repository remote is never inferred from Git.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".runtime" / "public-release"
STATE = WORK / "state.json"
SCOPE = ROOT / "scripts" / "public-release-scope.json"
MANIFEST = "PUBLIC_SNAPSHOT_MANIFEST.json"
MAX_FILE_BYTES = 5 * 1024 * 1024
BLOCKED = {
    ".git", ".runtime", ".cache", ".codex", ".claude", ".venv",
    "__pycache__", ".pytest_cache", "node_modules", "target", "dist",
    "build", "outputs", "review-bundles", "test-results", "playwright-report",
    "local-maintenance",
}
TEXT = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".css",
    ".html", ".java", ".xml", ".yml", ".yaml", ".properties",
    ".sql", ".sh", ".ps1", ".bat", ".json", ".jsonl", ".toml",
    ".md", ".txt", ".svg", ".gitignore", ".dockerignore",
}
BINARY = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp"}
SPECIAL = {"Dockerfile", ".gitignore", ".dockerignore", ".env.example", ".env.sample"}

sys.path.insert(0, str(ROOT / "scripts"))
from check_public_snapshot import SECRET_PATTERNS  # noqa: E402


def die(message: str) -> None:
    raise RuntimeError(message)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def files(root: Path) -> dict[str, Path]:
    return {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*")
        if path.is_file()
        and not {".git", ".runtime", "__pycache__", ".pytest_cache"}.intersection(path.relative_to(root).parts)
    }


def hashes(root: Path) -> dict[str, str]:
    return {name: sha256(path.read_bytes()) for name, path in files(root).items()}


def diff_files(previous: dict[str, str], current: dict[str, str]) -> tuple[list[str], list[str]]:
    changed = sorted(name for name, digest in current.items() if previous.get(name) != digest)
    removed = sorted(set(previous) - set(current))
    return changed, removed


def safe_source(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    name = path.name.lower()
    return (
        path.is_file()
        and not path.is_symlink()
        and not BLOCKED.intersection(relative.parts)
        and not (name == ".env" or (name.startswith(".env.") and name not in {".env.example", ".env.sample"}))
        and path.stat().st_size <= MAX_FILE_BYTES
    )


def copy_source(path: Path, stage: Path) -> None:
    if not safe_source(path):
        die(f"source file is unsafe or too large: {path.relative_to(ROOT)}")
    destination = stage / path.relative_to(ROOT)
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = path.read_bytes()
    if path.suffix.lower() in TEXT or path.name in SPECIAL:
        try:
            data = data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
        except UnicodeDecodeError:
            die(f"expected UTF-8 text: {path.relative_to(ROOT)}")
    destination.write_bytes(data)


def source_walk(root: Path):
    for current, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if name not in BLOCKED and not (Path(current) / name).is_symlink()]
        for name in names:
            yield Path(current) / name


def sync_tree(relative: str, stage: Path) -> None:
    source = ROOT / relative
    if not source.is_dir() or source.is_symlink():
        die(f"configured source tree is missing: {relative}")
    destination = (stage / relative).resolve()
    if not destination.is_relative_to(stage.resolve()):
        die(f"unsafe stage tree: {relative}")
    if destination.exists():
        shutil.rmtree(destination)
    for path in source_walk(source):
        if path.suffix.lower() in TEXT | BINARY or path.name in SPECIAL:
            copy_source(path, stage)


def local_secret_values() -> list[bytes]:
    environment = ROOT / ".env"
    if not environment.is_file():
        return []
    secrets = []
    for line in environment.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip("\"'")
        if (
            re.search(r"(TOKEN|SECRET|PASSWORD|API_KEY)", key, re.I)
            and len(value) >= 8
            and not value.startswith(("${", "your-", "change", "example", "public-demo"))
        ):
            secrets.append((key.strip(), value.encode("utf-8")))
    return [value for _, value in sorted(secrets)]


def redact_and_scan(stage: Path) -> int:
    values = local_secret_values()
    replacements = 0
    for name, path in files(stage).items():
        if name == MANIFEST:
            continue
        data = path.read_bytes()
        changed = data
        for index, value in enumerate(values, start=1):
            changed = changed.replace(value, f"public-demo-secret-{index}-change-before-use".encode())
        if changed != data:
            path.write_bytes(changed)
            replacements += 1
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(changed):
                die(f"candidate contains a {label}: {name}")
        if any(value in changed for value in values):
            die(f"candidate contains a local credential value: {name}")
    return replacements


def write_manifest(stage: Path) -> None:
    path = stage / MANIFEST
    if path.exists():
        path.unlink()
    entries = [
        {"path": name, "bytes": file.stat().st_size, "sha256": sha256(file.read_bytes())}
        for name, file in sorted(files(stage).items())
    ]
    write_json(path, {"schemaVersion": "public-snapshot-manifest-v1", "fileCount": len(entries), "files": entries})


def run_checks(stage: Path, scope: dict) -> dict:
    commands = [
        [sys.executable, "-B", "scripts/check_public_snapshot.py", str(stage)],
        [sys.executable, "-B", "scripts/check_repository_hygiene.py"],
        [sys.executable, "-B", "scripts/check_markdown_links.py"],
    ]
    tests = scope.get("tests", [])
    for relative in tests:
        if not (stage / relative).is_file():
            die(f"configured test is absent from snapshot: {relative}")
    if tests:
        commands.append([sys.executable, "-B", "-m", "pytest", "-q", *tests, "-p", "no:cacheprovider"])
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    for command in commands:
        process = subprocess.run(command, cwd=stage, env=environment, capture_output=True, text=True, timeout=300)
        print(process.stdout.strip(), flush=True)
        if process.returncode:
            die(f"check failed: {command[2]}\n{process.stderr[-2000:]}")
    return {"status": "PASS", "checks": len(commands)}


class GitHub:
    def __init__(self, repository: str):
        self.repository = repository
        token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=30)
        if token.returncode or not token.stdout.strip():
            die("GitHub authentication is unavailable; run gh auth login")
        self.token = token.stdout.strip()

    def api(self, route: str, payload: dict | None = None, method: str = "POST", *, retry: bool = True) -> dict:
        payload_path = None
        try:
            config = [
                f'url = "https://api.github.com/repos/{self.repository}/{route}"',
                'header = "Accept: application/vnd.github+json"',
                'header = "X-GitHub-Api-Version: 2022-11-28"',
                f'header = "Authorization: Bearer {self.token}"',
                "http1.1", "fail", "silent", "show-error", "max-time = 180",
            ]
            if payload is not None:
                with tempfile.NamedTemporaryFile(mode="wb", prefix="public-release-api-", suffix=".json", dir=WORK, delete=False) as handle:
                    payload_path = Path(handle.name)
                    handle.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                config.extend([f'request = "{method}"', 'header = "Content-Type: application/json"', f'data-binary = "@{payload_path.as_posix()}"'])
            config_bytes = ("\n".join(config) + "\n").encode("utf-8")
            attempts = 4 if retry else 1
            for attempt in range(attempts):
                response = subprocess.run(["curl.exe", "--config", "-"], input=config_bytes, capture_output=True, timeout=200)
                if response.returncode == 0:
                    return json.loads(response.stdout)
                error = response.stderr.decode(errors="replace")
                if response.returncode not in {18, 28, 35, 52, 55, 56} or attempt == attempts - 1:
                    die(f"GitHub API failed ({route}): {error[:1000]}")
                time.sleep(2 * (attempt + 1))
            die("unreachable GitHub API retry state")
        finally:
            if payload_path is not None:
                payload_path.unlink(missing_ok=True)

    def main_sha(self) -> str:
        return self.api("git/refs/heads/main")["object"]["sha"]


def scope_and_state() -> tuple[dict, dict]:
    scope = read_json(SCOPE)
    if scope.get("schemaVersion") != "public-release-scope-v1":
        die("invalid public release scope")
    state = read_json(STATE)
    if state.get("repository") != scope["repository"]:
        die("repository differs from initialized state")
    return scope, state


def verified_snapshot(snapshot: Path, expected: dict[str, str]) -> None:
    if not snapshot.is_dir():
        die(f"snapshot is absent: {snapshot}")
    actual = hashes(snapshot)
    if actual != expected:
        differences = sorted(name for name in set(actual) | set(expected) if actual.get(name) != expected.get(name))
        die(f"snapshot differs from the verified plan at {len(differences)} paths: {differences[:12]}")
    check = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/check_public_snapshot.py"), str(snapshot)], capture_output=True, text=True, timeout=60)
    if check.returncode:
        die(f"snapshot manifest failed: {check.stdout[-1500:]}{check.stderr[-500:]}")


def verified_base(snapshot: Path, state: dict) -> dict[str, str]:
    manifest_path = snapshot / MANIFEST
    if not manifest_path.is_file() or sha256(manifest_path.read_bytes()) != state.get("manifestSha256"):
        die("verified base manifest is missing or changed")
    manifest = read_json(manifest_path)
    declared = {entry["path"]: entry["sha256"] for entry in manifest["files"]}
    actual = hashes(snapshot)
    actual.pop(MANIFEST, None)
    if actual != declared:
        die("verified base files differ from their manifest")
    actual[MANIFEST] = sha256(manifest_path.read_bytes())
    return actual


def initialize(args) -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    if STATE.exists() and not args.replace:
        die("release state already exists; use init --replace with a newly verified remote snapshot")
    scope = read_json(SCOPE)
    snapshot = Path(args.snapshot).resolve()
    evidence = read_json(Path(args.evidence_plan))
    receipt = read_json(Path(args.receipt))
    if receipt.get("status") != "REMOTE_VERIFIED" or receipt.get("sha") != args.sha:
        die("receipt does not prove this published SHA")
    if evidence.get("files") is None or len(evidence["files"]) != receipt.get("files"):
        die("evidence plan and remote receipt disagree")
    verified_snapshot(snapshot, evidence["files"])
    remote = GitHub(scope["repository"]).main_sha()
    if remote != args.sha:
        die(f"GitHub main is {remote}, not the verified snapshot {args.sha}")
    write_json(STATE, {"schemaVersion": "public-release-state-v1", "repository": scope["repository"], "baseSha": args.sha, "snapshot": str(snapshot), "receipt": str(Path(args.receipt).resolve()), "manifestSha256": sha256((snapshot / MANIFEST).read_bytes())})
    print(json.dumps({"status": "INITIALIZED", "base": args.sha, "files": len(evidence["files"])}, ensure_ascii=False))


def build_stage(base: Path, stage: Path, scope: dict) -> int:
    if stage.exists():
        die(f"stage already exists: {stage}")
    shutil.copytree(base, stage)
    for relative in scope["syncTrees"]:
        sync_tree(relative, stage)
    for relative in scope["codeOverlays"]:
        source = ROOT / relative
        if not source.is_dir():
            die(f"configured overlay is missing: {relative}")
        for path in source_walk(source):
            if path.suffix.lower() in {".py", ".ps1", ".sh", ".bat"}:
                copy_source(path, stage)
    for relative in scope["topLevelOverlays"]:
        source = ROOT / relative
        if not source.is_dir():
            die(f"configured top-level overlay is missing: {relative}")
        for path in source.iterdir():
            if path.is_file() and (path.suffix.lower() in TEXT or path.name in SPECIAL):
                copy_source(path, stage)
    for relative in scope["requiredFiles"]:
        source = ROOT / relative
        if not source.is_file():
            die(f"required public source file is missing: {relative}")
        copy_source(source, stage)
    for relative in scope.get("optionalFiles", []):
        source = ROOT / relative
        if source.is_file():
            copy_source(source, stage)
    for folder, suffixes in (("db", {".sql", ".sh"}), ("observability", {".yml", ".yaml", ".json"})):
        root = ROOT / folder
        if root.is_dir():
            for path in source_walk(root):
                if path.suffix.lower() in suffixes:
                    copy_source(path, stage)
    replacements = redact_and_scan(stage)
    write_manifest(stage)
    return replacements


def plan(args) -> None:
    scope, state = scope_and_state()
    base = Path(state["snapshot"])
    if not base.is_dir():
        die("verified base snapshot is missing; reinitialize from remote evidence")
    previous = verified_base(base, state)
    remote = GitHub(scope["repository"]).main_sha()
    if remote != state["baseSha"]:
        die(f"GitHub main moved to {remote}; verified local base is {state['baseSha']}")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    run = WORK / "runs" / run_id
    run.mkdir(parents=True, exist_ok=False)
    stage = run / "stage"
    replacements = build_stage(base, stage, scope)
    current = hashes(stage)
    changed, removed = diff_files(previous, current)
    result = {"schemaVersion": "public-release-plan-v1", "repository": scope["repository"], "baseSha": remote, "stage": str(stage), "files": current, "changed": changed, "removed": removed, "allowDelete": bool(args.allow_delete), "redactedFiles": replacements}
    write_json(run / "plan.json", result)
    print(json.dumps({"changed": changed, "removed": removed, "redactedFiles": replacements}, ensure_ascii=False, indent=2), flush=True)
    if removed and not args.allow_delete:
        die(f"{len(removed)} public files would be deleted; review plan and rerun with --allow-delete if intentional")
    checks = run_checks(stage, scope)
    write_json(run / "checks.json", checks)
    print(json.dumps({"status": "PLAN_READY", "plan": str(run / 'plan.json'), "changed": len(changed), "removed": len(removed)}, ensure_ascii=False))


def plan_inputs(path: Path) -> tuple[dict, Path, Path]:
    plan_file = path.resolve()
    data = read_json(plan_file)
    if data.get("schemaVersion") != "public-release-plan-v1":
        die("invalid release plan")
    run = plan_file.parent
    stage = (run / "stage").resolve()
    if stage != Path(data["stage"]).resolve() or not stage.is_dir():
        die("plan stage identity mismatch")
    if read_json(run / "checks.json").get("status") != "PASS":
        die("plan checks are not PASS")
    verified_snapshot(stage, data["files"])
    return data, run, stage


def published_identity(gh: GitHub, sha: str, base: str, tree: str) -> None:
    commit = gh.api("git/commits/" + sha)
    if commit["tree"]["sha"] != tree or [parent["sha"] for parent in commit["parents"]] != [base]:
        die("remote commit tree or parent differs from the plan")


def publish(args) -> None:
    data, run, stage = plan_inputs(Path(args.plan))
    scope, state = scope_and_state()
    if data["repository"] != scope["repository"] or data["baseSha"] != state["baseSha"]:
        die("plan is stale or addresses another repository")
    if data["removed"] and not data["allowDelete"]:
        die("plan contains unapproved deletions")
    if not data["changed"] and not data["removed"]:
        die("nothing changed")
    gh = GitHub(scope["repository"])
    if gh.main_sha() != data["baseSha"]:
        die("GitHub main changed after planning")
    base_commit = gh.api("git/commits/" + data["baseSha"])
    entries = []
    uploaded = {}
    order = sorted(data["changed"], key=lambda name: (name == MANIFEST, (stage / name).stat().st_size))
    for index, name in enumerate(order, 1):
        content = (stage / name).read_bytes()
        try:
            payload = {"content": content.decode("utf-8"), "encoding": "utf-8"}
        except UnicodeDecodeError:
            payload = {"content": base64.b64encode(content).decode(), "encoding": "base64"}
        blob = gh.api("git/blobs", payload)
        if blob["sha"] != git_blob_sha(content):
            die(f"uploaded blob hash mismatch: {name}")
        entries.append({"path": name, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        uploaded[name] = blob["sha"]
        print(f"BLOB {index}/{len(order)}", flush=True)
    entries.extend({"path": name, "mode": "100644", "type": "blob", "sha": None} for name in data["removed"])
    tree = gh.api("git/trees", {"base_tree": base_commit["tree"]["sha"], "tree": entries})["sha"]
    commit = gh.api("git/commits", {"message": args.message, "tree": tree, "parents": [data["baseSha"]]})
    write_json(run / "pending.json", {"sha": commit["sha"], "tree": tree, "uploaded": uploaded})
    if gh.main_sha() != data["baseSha"]:
        die("GitHub main moved before publication")
    try:
        gh.api("git/refs/heads/main", {"sha": commit["sha"], "force": False}, method="PATCH", retry=False)
    except RuntimeError:
        if gh.main_sha() != commit["sha"]:
            raise
    if gh.main_sha() != commit["sha"]:
        die("GitHub main does not point to the new commit")
    write_json(run / "published.json", {"sha": commit["sha"], "base": data["baseSha"], "tree": tree})
    verify(argparse.Namespace(plan=args.plan))


def verify(args) -> None:
    data, run, stage = plan_inputs(Path(args.plan))
    pending = read_json(run / "pending.json")
    scope, state = scope_and_state()
    if state["baseSha"] not in {data["baseSha"], pending["sha"]}:
        die("state differs from the plan and published commit")
    gh = GitHub(scope["repository"])
    if gh.main_sha() != pending["sha"]:
        die("GitHub main does not point to the planned commit")
    published_identity(gh, pending["sha"], data["baseSha"], pending["tree"])
    for name, expected_sha in pending["uploaded"].items():
        response = gh.api("git/blobs/" + expected_sha)
        content = base64.b64decode(response["content"])
        if git_blob_sha(content) != expected_sha or sha256(content) != data["files"][name]:
            die(f"remote blob verification failed: {name}")
    deadline = time.monotonic() + 120
    while True:
        actions = gh.api(f"actions/runs?head_sha={pending['sha']}&per_page=20")
        runs = [item for item in actions.get("workflow_runs", []) if item.get("head_sha") == pending["sha"]]
        if runs and all(item.get("status") == "completed" for item in runs):
            break
        if time.monotonic() >= deadline:
            die("remote commit verified; GitHub Actions still pending. Rerun verify later")
        time.sleep(8)
    failed = [item["name"] for item in runs if item.get("conclusion") != "success"]
    if failed:
        die("GitHub Actions failed: " + ", ".join(failed))
    receipt = {"status": "REMOTE_VERIFIED", "sha": pending["sha"], "base": data["baseSha"], "tree": pending["tree"], "files": len(data["files"]), "changed": len(data["changed"]), "removed": len(data["removed"]), "actions": {item["name"]: item["conclusion"] for item in runs}}
    write_json(run / "receipt.json", receipt)
    write_json(STATE, {"schemaVersion": "public-release-state-v1", "repository": scope["repository"], "baseSha": pending["sha"], "snapshot": str(stage), "receipt": str(run / "receipt.json"), "manifestSha256": sha256((stage / MANIFEST).read_bytes())})
    print(json.dumps(receipt, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    initial = sub.add_parser("init", help="initialize from an already verified public snapshot")
    initial.add_argument("--snapshot", required=True)
    initial.add_argument("--sha", required=True)
    initial.add_argument("--evidence-plan", required=True)
    initial.add_argument("--receipt", required=True)
    initial.add_argument("--replace", action="store_true")
    proposal = sub.add_parser("plan", help="build and validate an immutable publication plan")
    proposal.add_argument("--allow-delete", action="store_true")
    sending = sub.add_parser("publish", help="publish an audited plan")
    sending.add_argument("--plan", required=True)
    sending.add_argument("--message", required=True)
    checking = sub.add_parser("verify", help="finish verification after a network interruption")
    checking.add_argument("--plan", required=True)
    args = parser.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    {"init": initialize, "plan": plan, "publish": publish, "verify": verify}[args.command](args)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"PUBLIC_RELEASE_ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
