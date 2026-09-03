"""Verify a generated public snapshot without trusting the source worktree."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path, PurePosixPath


MANIFEST_NAME = "PUBLIC_SNAPSHOT_MANIFEST.json"
MAX_FILE_BYTES = 5 * 1024 * 1024
FORBIDDEN_PARTS = {
    ".git",
    ".runtime",
    ".cache",
    ".codex",
    ".claude",
    ".idea",
    ".vscode",
    "__pycache__",
    ".pytest_cache",
    "target",
    "node_modules",
    "review-bundles",
    "outputs",
}
SECRET_PATTERNS = {
    "private key": re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    "OpenAI-style API key": re.compile(rb"\bsk-[A-Za-z0-9_-]{32,}\b"),
    "GitHub token": re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "AWS access key": re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot", type=Path)
    args = parser.parse_args()
    root = args.snapshot.resolve()
    if not root.is_dir():
        raise SystemExit(f"snapshot directory does not exist: {root}")

    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise SystemExit(f"missing {MANIFEST_NAME}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if manifest.get("schemaVersion") != "public-snapshot-manifest-v1":
        raise SystemExit("invalid public snapshot manifest schema")

    failures: list[str] = []
    actual_files: dict[str, Path] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if FORBIDDEN_PARTS.intersection(relative.parts):
            # Tests may create local bytecode/pytest caches after the immutable
            # snapshot manifest is generated. They are never public assets.
            continue
        if path.is_symlink():
            failures.append(f"{relative.as_posix()}: symlink is forbidden")
            continue
        if not path.is_file() or path.name == MANIFEST_NAME:
            continue
        display = relative.as_posix()
        actual_files[display] = path
        lower_name = relative.name.lower()
        if lower_name == ".env" or (
            lower_name.startswith(".env.")
            and lower_name not in {".env.example", ".env.sample"}
        ):
            failures.append(f"{display}: environment file is forbidden")
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            failures.append(f"{display}: exceeds 5 MiB")
            continue
        content = path.read_bytes()
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(content):
                failures.append(f"{display}: looks like a real {label}")

    entries = manifest.get("files")
    if not isinstance(entries, list):
        failures.append("manifest files must be a list")
        entries = []
    declared: dict[str, dict[str, object]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            failures.append("manifest contains an invalid file entry")
            continue
        relative = entry["path"]
        if relative in declared:
            failures.append(f"{relative}: duplicate manifest entry")
            continue
        if FORBIDDEN_PARTS.intersection(PurePosixPath(relative).parts):
            failures.append(f"{relative}: manifest declares a forbidden path")
        declared[relative] = entry

    if set(actual_files) != set(declared):
        missing = sorted(set(declared) - set(actual_files))
        extra = sorted(set(actual_files) - set(declared))
        if missing:
            failures.append("manifest paths missing from snapshot: " + ", ".join(missing))
        if extra:
            failures.append("snapshot paths missing from manifest: " + ", ".join(extra))

    for relative in sorted(set(actual_files) & set(declared)):
        path = actual_files[relative]
        entry = declared[relative]
        if entry.get("bytes") != path.stat().st_size:
            failures.append(f"{relative}: size mismatch")
        if entry.get("sha256") != sha256(path):
            failures.append(f"{relative}: SHA-256 mismatch")

    if manifest.get("fileCount") != len(entries):
        failures.append("manifest fileCount mismatch")
    if failures:
        print(json.dumps({"status": "FAIL", "failures": failures}, ensure_ascii=False, indent=2))
        return 1
    print(
        json.dumps(
            {
                "status": "PASS",
                "fileCount": len(actual_files),
                "secretMatches": 0,
                "oversizedFiles": 0,
                "manifestMismatches": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
