"""Fail when tracked files contain common secret or repository-hygiene hazards."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAX_TRACKED_BYTES = 5 * 1024 * 1024
GENERATED_PARTS = {
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "dist",
    "node_modules",
    "target",
}
SECRET_PATTERNS = {
    "private key": re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    "OpenAI-style API key": re.compile(rb"\bsk-[A-Za-z0-9_-]{32,}\b"),
    "GitHub token": re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "AWS access key": re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
}


def tracked_files() -> list[PurePosixPath]:
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", "ls-files", "-z"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
    )
    return [
        PurePosixPath(item.decode("utf-8", errors="surrogateescape"))
        for item in result.stdout.split(b"\0")
        if item
    ]


def repository_files() -> list[PurePosixPath]:
    """Use Git's index in the worktree, or the filesystem in a clean snapshot."""

    if (PROJECT_ROOT / ".git").exists():
        return tracked_files()
    return sorted(
        PurePosixPath(path.relative_to(PROJECT_ROOT).as_posix())
        for path in PROJECT_ROOT.rglob("*")
        if path.is_file()
        and not GENERATED_PARTS.intersection(path.relative_to(PROJECT_ROOT).parts)
    )


def is_forbidden_env_file(path: PurePosixPath) -> bool:
    name = path.name.lower()
    if name in {".env.example", ".env.sample"}:
        return False
    return name == ".env" or name.startswith(".env.")


def main() -> int:
    failures: list[str] = []
    for relative in repository_files():
        display = relative.as_posix()
        if is_forbidden_env_file(relative):
            failures.append(f"{display}: tracked environment file")
        if GENERATED_PARTS.intersection(relative.parts):
            failures.append(f"{display}: tracked generated/dependency directory")

        absolute = PROJECT_ROOT.joinpath(*relative.parts)
        if absolute.is_symlink():
            failures.append(f"{display}: symlink is not allowed in a public snapshot")
            continue
        try:
            size = absolute.stat().st_size
        except FileNotFoundError:
            continue
        if size > MAX_TRACKED_BYTES:
            failures.append(
                f"{display}: {size / (1024 * 1024):.2f} MiB exceeds 5 MiB"
            )
            continue

        content = absolute.read_bytes()
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(content):
                failures.append(f"{display}: looks like a real {label}")

    if failures:
        print("Repository hygiene check failed:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("Repository hygiene check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
