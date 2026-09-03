"""Generate deterministic source and package checksums after final reporting."""

from __future__ import annotations

import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = Path(__file__).resolve().parent
SOURCE_HASHES = PACKAGE_DIR / "SOURCE_HASHES.sha256"
PACKAGE_HASHES = PACKAGE_DIR / "SHA256SUMS.txt"

SOURCE_PATHS = (
    "agent/evaluation/context_compiler_provider_paired_v1_20260901_v4/runner.py",
    "agent/evaluation/context_compiler_provider_paired_v1_20260901_v4/build_package.py",
    "agent/evaluation/context_compiler_provider_paired_v1_20260901_v4/verify_package.py",
    "agent/evaluation/context_compiler_provider_paired_v1_20260901_v4/finalize_hashes.py",
    "agent/evaluation/context_compiler_provider_paired_v1_20260901_v4/preregistration.md",
    "agent/evaluation/context_compiler_provider_paired_v1_20260901_v1/runner.py",
    "agent/evaluation/context_compiler_provider_paired_v1_20260901_v3/runner.py",
    "agent/app/context_compiler_v1.py",
    "agent/app/reference_context.py",
    "agent/tests/test_context_compiler_provider_paired_v1.py",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    SOURCE_HASHES.write_text(
        "".join(f"{sha256(ROOT / relative)}  {relative}\n" for relative in SOURCE_PATHS),
        encoding="utf-8",
        newline="\n",
    )
    package_files = sorted(
        path
        for path in PACKAGE_DIR.rglob("*")
        if path.is_file()
        and path != PACKAGE_HASHES
        and "__pycache__" not in path.parts
    )
    PACKAGE_HASHES.write_text(
        "".join(
            f"{sha256(path)}  {path.relative_to(PACKAGE_DIR).as_posix()}\n"
            for path in package_files
        ),
        encoding="utf-8",
        newline="\n",
    )
    print(f"sourceHashCount={len(SOURCE_PATHS)} packageHashCount={len(package_files)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

